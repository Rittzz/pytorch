import functools
from typing import Callable, Dict

import torch
import torch.distributed as dist
import torch.distributed._shard.sharding_spec as shard_spec
from torch.distributed import distributed_c10d
from torch.distributed._shard.sharded_tensor.api import ShardedTensor
from torch.distributed.nn.functional import (
    reduce_scatter,
)

# Custom PartialTensor ops
_PARTIAL_TENSOR_OPS: Dict[Callable, Callable] = {}
def _register_partial_tensor_op(op, func):
    from inspect import signature
    if len(signature(func).parameters) != 3:
        raise TypeError(
            f'Partial tensor op function expects signature: '
            f'(types, args, kwargs), but received '
            f'signature: {signature(func)}')

    global _PARTIAL_TENSOR_OPS
    _PARTIAL_TENSOR_OPS[op] = func

def _custom_partial_tensor_op(func):
    """
    Decorate for custom partial tensor op
    Args:
        func(Callable): Torch function for which we want to provide a PartialTensor
            implementation (ex: torch.nn.functional.linear)
    """
    def decorator_sharded_func(wrapped_func):
        _register_partial_tensor_op(func, wrapped_func)

        @functools.wraps(wrapped_func)
        def wrapper(*args, **kwargs):
            return wrapped_func(*args, **kwargs)
        return wrapper
    return decorator_sharded_func

class _PartialTensor(torch.Tensor):
    """
    PartialTensor is an abstraction to represent Tensors that need
    aggregation across multiple devices and multiple processes.

    PartialTensor is initialized in an SPMD like fashion where each rank
    initializes the PartialTensor. The PartialTensor object on each rank
    then only stores the local partial shard, process group and the
    aggregation way to get a full tensor.

    PartialTensor doesn't provide any Tensor like operations but is a
    wrapper providing the Tensor representing the local partial shard.

    We assume the size of each local tensor to be exactly the same.

    Users can apply custom distributed sharded computations on top of
    this primitive.

    Args:
        local_partial_shard (Tensor): Partial result stored across ranks.
        process_group (ProcessGroup): The process group to aggregate on.
        reduce_op (distributed_c10d.ReduceOp): Way to aggregate the partial result.
            Default: ``distributed_c10d.ReduceOp.SUM``

    Examples:
        >>> # All tensors below are of torch.int64 type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.arange(2, dtype=torch.int64) + 1 + 2 * rank
        >>> tensor = torch.cat([tensor, tensor + 2])
        >>> tensor
        tensor([1, 2, 3, 4]) # Rank 0
        tensor([3, 4, 5, 6]) # Rank 1
        >>> partial_tensor = _PartialTensor(tensor, distributed_c10d.ReduceOp.MAX)
        >>> sharding_dim = 0
        >>> collect_spec = shard_spec.ChunkShardingSpec(
                dim=sharding_dim,
                placements=[
                    "rank:0/cuda:0",
                    "rank:1/cuda:1",
                ],
            )
        >>> complete_tensor = partial_tensor.reshard(collect_spec)
        >>> complete_tensor
        ShardedTensor(
            ShardedTensorMetadata(
                shards_metadata=[
                    ShardMetadata(shard_offsets=[0], shard_sizes=[2], placement=rank:0/cuda:0),
                    ShardMetadata(shard_offsets=[2], shard_sizes=[2], placement=rank:1/cuda:1)],
                size=torch.Size([4])
        )
        >>> complete_tensor.local_tensor()
        tensor([3, 4]) # Rank 0
        tensor([5, 6]) # Rank 1

        >>> # All tensors below are of torch.cfloat type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.tensor([1, 2]) + 2 * rank
        >>> tensor = torch.cat([tensor, tensor + 2])
        >>> tensor
        tensor([1, 2, 3, 4]) # Rank 0
        tensor([3, 4, 5, 6]) # Rank 1
        >>> partial_tensor = _PartialTensor(tensor)
        >>> complete_tensor = partial_tensor.reshard(collect_spec)
        >>> complete_tensor
        ShardedTensor(
            ShardedTensorMetadata(
                shards_metadata=[
                    ShardMetadata(shard_offsets=[0], shard_sizes=[2], placement=rank:0/cuda:0),
                    ShardMetadata(shard_offsets=[2], shard_sizes=[2], placement=rank:1/cuda:1)],
                size=torch.Size([4])
        )
        >>> complete_tensor.local_tensor()
        tensor([4, 6]) # Rank 0
        tensor([8, 10]) # Rank 1
    """

    _process_group: distributed_c10d.ProcessGroup
    _local_shard: torch.Tensor
    _reduce_op: distributed_c10d.ReduceOp

    __slots__ = ["_process_group", "_local_shard", "_reduce_op"]

    def __new__(cls, local_shard, process_group=None, reduce_op=distributed_c10d.ReduceOp.SUM):
        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            local_shard.size(),
            dtype=local_shard.dtype,
            layout=local_shard.layout,
            pin_memory=local_shard.is_pinned(),
            requires_grad=local_shard.requires_grad)      # type: ignore[arg-type]
        r._process_group = (     # type: ignore[attr-defined]
            process_group
            if process_group is not None
            else distributed_c10d._get_default_group()
        )
        r._reduce_op = reduce_op
        r._local_shard = local_shard
        return r

    def __post_init__(self):
        if not isinstance(self._reduce_op, distributed_c10d.ReduceOp):
            raise ValueError(
                "reduce_op needs to be a member of distributed_c10d.ReduceOp."
            )

    def reshard(self, resharding_spec: shard_spec.ShardingSpec) -> ShardedTensor:
        """
        The reshard happens in two steps logically:

        1. Aggregate all the shards of the partial tensor.
        2. Shard this tensor according to the provided spec.

        In reality, for the sake of performance, we consolidate all partial tensors
        across multiple ranks and covert to a sharded tensor in one step.

        Args:
            resharding_spec (:class:`torch.distributed._shard.sharding_spec.ShardingSpec`):
                The specification describing how we reshard the aggregated local result.

        Returns:
            A :class:`ShardedTensor` filled with local aggregated result.
        """
        if not isinstance(resharding_spec, shard_spec.ChunkShardingSpec):
            raise NotImplementedError("Only ChunkShardingSpec supported for reshard.")
        if self._local_shard.is_complex():
            raise NotImplementedError("Only real partial tensor supported for reshard.")
        sharding_dim = int(resharding_spec.dim)  # type: ignore[attr-defined]
        chunk_mode_res = self._local_shard.size(sharding_dim) % self._process_group.size()
        local_shard = self._local_shard
        # Add padding when the size is not divisible by the world size.
        if chunk_mode_res != 0:
            padding = [0] * (local_shard.dim() * 2)
            padding[-1] = self._process_group.size() - chunk_mode_res
            local_shard = torch.nn.functional.pad(
                local_shard,
                tuple(padding),
                "constant",
                0,
            )
        current_rank = dist.get_rank(self._process_group)  # type: ignore[attr-defined]
        rank_idx = None
        rearrange_local_shards = False
        indices = [0] * self._process_group.size()
        for idx, placement in enumerate(resharding_spec.placements):  # type: ignore[attr-defined]
            if placement.rank() == current_rank:  # type: ignore[index, union-attr]
                rank_idx = idx  # type: ignore[attr-defined]
            if placement.rank() != idx:  # type: ignore[index, union-attr]
                rearrange_local_shards = True
            indices[placement.rank()] = idx  # type: ignore[index, union-attr]

        local_shards = local_shard.chunk(self._process_group.size(), dim=sharding_dim)
        if rearrange_local_shards:
            # Need to re-arrange original shard_dim of output_tensor_list.
            local_shards = [local_shards[idx] for idx in indices]  # type: ignore[call-overload]
        local_result = reduce_scatter(
            torch.empty_like(local_shards[0]), list(local_shards), op=self._reduce_op
        )

        sharded_tensor_size = self._local_shard.size()
        # Remove padding when the size is not divisible by the world size.
        if chunk_mode_res != 0:
            uneven_local_shards = self._local_shard.chunk(
                self._process_group.size(), dim=sharding_dim
            )
            expected_size = uneven_local_shards[rank_idx].size()  # type: ignore[index]
            if local_result.size() != expected_size:
                local_result = local_result.narrow(
                    sharding_dim,
                    0,
                    expected_size[sharding_dim],
                )
        return ShardedTensor._init_from_local_tensor(
            local_result,
            resharding_spec,
            sharded_tensor_size,
            process_group=self._process_group,
        )

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if func in _PARTIAL_TENSOR_OPS:
            return _PARTIAL_TENSOR_OPS[func](types, args, kwargs)

        # Need to disable all dispatch to print args and kwargs appropriately.
        guard = torch._C._DisableTorchDispatch()  # type: ignore[attr-defined]
        try:
            with torch._C.DisableTorchFunction():
                raise RuntimeError(
                    f"torch function '{func.__name__}', with args: {args} and "
                    f"kwargs: {kwargs} not supported for PartialTensor!")
        finally:
            del guard

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        raise RuntimeError(
            f"A {cls.__name__} object is being used from c++ "
            f"while calling {func.__module__}.{func.__name__} "
            "but the there is no custom __torch_dispatch__ implementation for it."
        )

    def __repr__(self):
        return f"PartialTensor({super(_PartialTensor, self).__repr__()})"

def _transpose_impl(types, args=(), kwargs=None):
    partial_tensor = args[0]
    input = partial_tensor._local_shard
    dim0 = args[1]
    dim1 = args[2]
    return _PartialTensor(
        torch.transpose(input, dim0, dim1),
        partial_tensor._process_group,
        partial_tensor._reduce_op
    )

@_custom_partial_tensor_op(torch.Tensor.transpose)
def partial_transpose(types, args=(), kwargs=None):
    return _transpose_impl(types, args, kwargs)

@_custom_partial_tensor_op(torch.transpose)
def partial_torch_transpose(types, args=(), kwargs=None):
    return _transpose_impl(types, args, kwargs)

@_custom_partial_tensor_op(torch.cat)
def partial_cat(types, args=(), kwargs=None):
    input_list = args[0]
    if len(input_list) == 0:
        raise RuntimeError('Empty list of tensors to torch.cat!')

    local_shards = []
    for idx, input in enumerate(input_list):
        if not isinstance(input, _PartialTensor):
            raise RuntimeError('All inputs need to be an instance of _PartialTensor')
        if idx == 0:
            reduce_op = input._reduce_op
        elif reduce_op != input._reduce_op:
            raise RuntimeError(
                'All _PartialTensor reduce_ops need to be the same, found: '
                '{reduce_op} and {input._reduce_op}'
            )

        local_shards.append(input._local_shard)

    if kwargs is None:
        dim = 0
    else:
        if 'out' in kwargs:
            raise RuntimeError('"out" kwarg is not supported!')
        dim = kwargs['dim'] if 'dim' in kwargs else 0

    return _PartialTensor(torch.cat(local_shards, dim), input._process_group, input._reduce_op)

@_custom_partial_tensor_op(torch.Tensor.size)
def partial_size(types, args=(), kwargs=None):
    if kwargs is None:
        kwargs = {}
    with torch._C.DisableTorchFunction():
        return torch.Tensor.size(*args, **kwargs)
