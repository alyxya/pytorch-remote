# Copyright (C) 2025 alyxya
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
from typing import Any, Callable, Dict, Optional, Union

import torch

from .constants import REMOTE_DEVICE_TYPE, CPU_DEVICE_TYPE, META_DEVICE_TYPE, PRIVATEUSE1_DISPATCH_KEY


log = logging.getLogger(__name__)

from ._device_daemon import driver
from ._meta_parser import prepare_for_sending


# View operations that should be handled locally with shared storage IDs
VIEW_OPERATIONS = {
    "aten.view.default",
    "aten.view",
    "aten::view",
    "aten.as_strided.default",
    "aten.as_strided",
}


# Lazy import to avoid import errors if remote execution is not available
def _get_remote_orchestrator() -> Optional[Any]:
    """Get the global remote orchestrator instance.

    The remote orchestrator handles communication with remote GPU machines
    and coordinates tensor operations across devices. This function
    imports the orchestrator and gracefully handles cases where remote
    execution is not available.

    Returns:
        RemoteOrchestrator instance if available, None otherwise
    """
    try:
        from ._remote_orchestrator import remote_orchestrator
        return remote_orchestrator
    except ImportError as e:
        log.warning(f"Remote execution not available: {e}")
        return None


def _handle_view_operation(op: torch._ops.OpOverload, *args: Any, **kwargs: Any) -> torch.Tensor:
    """
    Handle view operations locally with shared storage IDs.

    View operations create new tensor views that share the same remote storage,
    updating only local metadata (shape, stride, storage_offset).
    """
    op_name = op.overloadpacket._qualified_op_name
    log.info(f"🔍 Handling view operation: {op_name}")

    # Get the base tensor (first argument for most view operations)
    base_tensor = args[0]

    if base_tensor.device.type != REMOTE_DEVICE_TYPE:
        # Not a remote tensor - execute normally
        return op(*args, **kwargs)

    # Get base tensor's storage ID (this is what data_ptr() returns)
    storage_id = base_tensor.untyped_storage().data_ptr()

    # Verify storage exists on a remote device
    device_idx = driver.exec("get_storage_device", storage_id)
    if device_idx is None:
        log.warning(f"No device found for storage {storage_id}, falling back to normal execution")
        return op(*args, **kwargs)

    # Execute the view operation on a CPU meta tensor to get the new shape/stride)
    # This gives us the correct metadata without doing actual computation
    meta_tensor = torch.empty(base_tensor.size(), dtype=base_tensor.dtype, device=META_DEVICE_TYPE)
    meta_result = op(meta_tensor, *args[1:], **kwargs)

    # Extract new tensor metadata
    new_shape = meta_result.size()
    new_stride = meta_result.stride()
    new_storage_offset = meta_result.storage_offset()

    # Now that we have C++ implementations for view operations,
    # we can use PyTorch's native as_strided which will call our C++ implementation
    result = torch.as_strided(base_tensor, new_shape, new_stride, new_storage_offset)

    # Verify the view was created correctly
    assert result.size() == new_shape, f"View shape mismatch: expected {new_shape}, got {result.size()}"
    assert result.stride() == new_stride, f"View stride mismatch: expected {new_stride}, got {result.stride()}"
    assert result.storage_offset() == new_storage_offset, f"View offset mismatch: expected {new_storage_offset}, got {result.storage_offset()}"
    assert result.untyped_storage().data_ptr() == base_tensor.untyped_storage().data_ptr(), "View should share storage"

    # PyTorch automatically manages storage reference counting
    log.info(f"✅ Created view tensor sharing storage {storage_id} for {op_name}")
    return result


def _remote_kernel_fallback(op: torch._ops.OpOverload, *args: Any, **kwargs: Any) -> Any:
    log.info("Calling kernel %s", op)

    # Handle operations using pytorch-openreg-2 logic for operation classification
    # but with remote execution for actual computation

    # First check for inplace operations (mutable)
    if op._schema.is_mutable or op is torch.ops.aten._copy_from.default:
        # Inplace operations - execute remotely and return the mutated tensor
        op_name = op.overloadpacket._qualified_op_name
        log.info(f"🔄 Inplace operation: {op_name}")

        # For inplace ops, the result is the first argument (mutated in place)
        result_tensor = args[0]

        # Execute remotely using efficient tensor ID system
        orchestrator = _get_remote_orchestrator()
        if orchestrator is not None:
            log.info(f"🚀 Executing inplace operation {op_name} remotely (efficient)")
            return orchestrator.execute_remote_aten_operation_efficient(op_name, args, kwargs)
        else:
            raise RuntimeError(f"Cannot execute inplace operation {op_name}: remote execution not available")

    # Handle as_strided separately from other view operations
    elif op is torch.ops.aten.as_strided.default:
        # as_strided should be handled by C++ but if it reaches here,
        # treat it as a regular operation (not a view operation)
        log.warning(f"as_strided reached Python fallback, executing remotely")
        op_name = op.overloadpacket._qualified_op_name
        log.info(f"🔧 as_strided operation: {op_name}")

        # Execute remotely using efficient storage ID system
        orchestrator = _get_remote_orchestrator()
        if orchestrator is not None:
            log.info(f"🚀 Executing as_strided operation {op_name} remotely (efficient)")
            return orchestrator.execute_remote_aten_operation_efficient(op_name, args, kwargs)
        else:
            raise RuntimeError(f"Cannot execute operation {op_name}: remote execution not available")

    # Second check for view operations (alias_info) - excluding as_strided
    elif any(r.alias_info is not None for r in op._schema.returns):
        # View ops - handle consistently using the view handler
        return _handle_view_operation(op, *args, **kwargs)

    # Everything else is a regular operation - execute remotely
    else:
        op_name = op.overloadpacket._qualified_op_name
        log.info(f"🔧 Regular operation: {op_name}")

        # Execute remotely using efficient tensor ID system
        orchestrator = _get_remote_orchestrator()
        if orchestrator is not None:
            log.info(f"🚀 Executing regular operation {op_name} remotely (efficient)")
            return orchestrator.execute_remote_aten_operation_efficient(op_name, args, kwargs)
        else:
            raise RuntimeError(f"Cannot execute operation {op_name}: remote execution not available")


def copy_from_device(from_: torch.Tensor) -> torch.Tensor:
    """Copy data from remote tensor to CPU tensor using remote execution"""
    if from_.device.type != REMOTE_DEVICE_TYPE:
        raise ValueError("copy_from_device requires a remote tensor")

    # Use remote execution to get the tensor data
    orchestrator = _get_remote_orchestrator()
    if orchestrator is not None:
        from .device import get_device_registry

        # Get the device backend
        registry = get_device_registry()
        device = registry.get_device_by_index(from_.device.index)

        if device is None:
            raise RuntimeError(f"No RemoteMachine found for remote device index {from_.device.index}")

        # Get the GPU machine for this device
        gpu_machine = device.get_gpu_machine()
        if gpu_machine is None or not gpu_machine.is_running():
            raise RuntimeError(f"GPU machine not available for device {device.machine_id}")

        # Get storage data using storage ID (convert int to string for GPU machine)
        storage_id_int = from_.untyped_storage().data_ptr()
        storage_id_str = str(storage_id_int)
        log.info(f"Copying storage ID {storage_id_int} from remote to CPU")

        # Use GPU machine to get tensor data by storage ID with view information
        # Pass tensor metadata so remote side can serialize just the view's data
        tensor_data = gpu_machine.get_storage_data(
            storage_id_str,
            shape=list(from_.shape),
            stride=list(from_.stride()),
            storage_offset=from_.storage_offset(),
            dtype=str(from_.dtype)
        )

        # Deserialize the tensor data as contiguous representation
        # Since we now serialize with .contiguous(), the deserialized tensor contains exactly
        # the data that should be in the result tensor - no view reconstruction needed
        result = orchestrator._deserialize_tensor(tensor_data)
        # Verify the result has the expected shape (it should match the remote tensor's shape)
        if result.size() != from_.size():
            log.warning(f"Deserialized tensor shape {result.size()} doesn't match remote tensor shape {from_.size()}")

        log.info(f"Successfully copied contiguous tensor data for storage ID {storage_id_int} to CPU")
        return result
    else:
        raise RuntimeError("Cannot copy from remote device: remote execution not available")


def copy_from_host_to_device(from_: torch.Tensor, to_: torch.Tensor) -> torch.Tensor:
    """Copy data from CPU tensor to remote tensor using remote execution"""
    if to_.device.type != REMOTE_DEVICE_TYPE:
        raise ValueError("copy_from_host_to_device requires a remote target tensor")
    if from_.device.type != CPU_DEVICE_TYPE:
        raise ValueError("copy_from_host_to_device requires a CPU source tensor")

    # Use remote execution to send the tensor data
    orchestrator = _get_remote_orchestrator()
    if orchestrator is not None:
        from .device import get_device_registry

        # Get the device backend
        registry = get_device_registry()
        device = registry.get_device_by_index(to_.device.index)

        if device is None:
            raise RuntimeError(f"No RemoteMachine found for remote device index {to_.device.index}")

        # Get the GPU machine for this device
        gpu_machine = device.get_gpu_machine()
        if gpu_machine is None or not gpu_machine.is_running():
            raise RuntimeError(f"GPU machine not available for device {device.machine_id}")

        # Send tensor data using tensor ID (convert int to string for GPU machine)
        storage_id_int = to_.untyped_storage().data_ptr()
        storage_id_str = str(storage_id_int)
        log.info(f"Copying CPU tensor to remote tensor ID {storage_id_int}")

        # Serialize the CPU tensor
        tensor_data = orchestrator._serialize_tensor(from_)
        # Use GPU machine to create/update tensor with specific ID
        # This will overwrite any existing empty tensor with the actual data
        created_id = gpu_machine.create_storage(tensor_data, storage_id_str)
        log.info(f"Successfully created/updated remote tensor with ID {created_id}")
        return to_
    else:
        raise RuntimeError("Cannot copy to remote device: remote execution not available")


def _copy_from(from_: torch.Tensor, to_: torch.Tensor) -> torch.Tensor:
    """Copy data from one tensor to another, handling remote device transfers.

    This function implements the core copy operation for remote tensors,
    supporting CPU↔remote transfers and preventing cross-device transfers
    between different remote devices.

    Args:
        from_: Source tensor to copy from
        to_: Target tensor to copy to

    Returns:
        Target tensor with copied data

    Raises:
        RuntimeError: If attempting to transfer between different remote devices
    """
    # Simplified copy implementation - remote tensors are now regular torch.Tensor
    # with proper device handling via C++ allocator

    # Preserve requires_grad property from source tensor
    should_preserve_grad = from_.requires_grad

    if from_.device.type == to_.device.type:
        if from_.device.type == REMOTE_DEVICE_TYPE:
            if from_.device.index == to_.device.index:
                # Same remote device - use direct copy
                op = torch.ops.aten.copy_.default
                result = _remote_kernel_fallback(op, to_, from_)
            else:
                # Different remote devices: NOT ALLOWED
                from torch_remote.device import get_device_registry
                device_registry = get_device_registry()
                from_device = device_registry.get_device_by_index(from_.device.index)
                to_device = device_registry.get_device_by_index(to_.device.index)

                raise RuntimeError(
                    f"Cannot transfer tensor between different remote devices. "
                    f"Source device: \"{from_device.machine_id}\" (index {from_.device.index}), "
                    f"Target device: \"{to_device.machine_id}\" (index {to_.device.index}). "
                    f"Transfer tensors to CPU first: tensor.cpu().to(target_device)"
                )
        else:
            # Both tensors on same non-remote device
            result = to_.copy_(from_)
    elif from_.device.type == REMOTE_DEVICE_TYPE:
        # Remote to non-remote
        host_mem = copy_from_device(from_)
        result = to_.copy_(host_mem)
    elif to_.device.type == REMOTE_DEVICE_TYPE:
        # Non-remote to remote
        result = copy_from_host_to_device(from_, to_)
    else:
        # Both non-remote but different devices
        result = to_.copy_(from_)

    # Preserve autograd properties
    if should_preserve_grad and not result.requires_grad:
        result.requires_grad_(True)

    return result


def _to_copy(input: torch.Tensor, *, dtype: Optional[torch.dtype] = None, layout: Optional[torch.layout] = None, device: Optional[Union[torch.device, str, int]] = None, pin_memory: Optional[bool] = None, non_blocking: bool = False, memory_format: Optional[torch.memory_format] = None) -> torch.Tensor:
    """Implementation of tensor.to() for remote tensors with cross-device transfer restriction."""

    # Handle device-specific transfers first
    if device is not None:
        target_device = torch.device(device) if not isinstance(device, torch.device) else device

        # Different device transfer - check if both are remote
        if input.device.type == REMOTE_DEVICE_TYPE and target_device.type == REMOTE_DEVICE_TYPE and input.device != target_device:
            # Cross-device remote transfer - NOT ALLOWED
            from torch_remote.device import get_device_registry
            device_registry = get_device_registry()
            from_device = device_registry.get_device_by_index(input.device.index)
            to_device = device_registry.get_device_by_index(target_device.index)

            raise RuntimeError(
                f"Cannot transfer tensor between different remote devices. "
                f"Source device: \"{from_device.machine_id}\" (index {input.device.index}), "
                f"Target device: \"{to_device.machine_id}\" (index {target_device.index}). "
                f"Transfer tensors to CPU first: tensor.cpu().to(target_device)"
            )

    # For all other cases, create a new tensor and use _copy_from if needed
    # This avoids infinite recursion by not calling back to the kernel fallback

    # Determine output parameters
    output_dtype = dtype if dtype is not None else input.dtype
    output_layout = layout if layout is not None else input.layout
    output_device = torch.device(device) if device is not None else input.device
    output_memory_format = memory_format if memory_format is not None else torch.contiguous_format

    # Create output tensor
    if output_device.type == REMOTE_DEVICE_TYPE:
        # Create empty remote tensor - use contiguous format for remote tensors
        result = torch.empty(input.size(), dtype=output_dtype, layout=output_layout,
                             device=output_device, memory_format=torch.contiguous_format)
    else:
        # Create empty tensor on target device
        result = torch.empty(input.size(), dtype=output_dtype, layout=output_layout,
                             device=output_device, memory_format=output_memory_format)

    # Copy data if needed (different device or same device but different dtype)
    if input.device != output_device or input.dtype != output_dtype:
        # Use _copy_from to handle the actual data transfer
        result = _copy_from(input, result)
    else:
        # Same device and dtype - just return input (no copy needed)
        return input

    return result


def _set_source_tensor(ten1: torch.Tensor, ten2: torch.Tensor) -> torch.Tensor:
    """Set one tensor to point to another tensor's storage.

    This creates a view relationship where ten1 shares ten2's storage,
    shape, stride, and offset. Used for tensor aliasing operations.

    Args:
        ten1: Tensor to modify
        ten2: Source tensor to point to

    Returns:
        Modified tensor ten1 pointing to ten2's data
    """
    return torch.ops.aten.set_.source_Storage_storage_offset(
        ten1,
        ten2.untyped_storage(),
        ten2.storage_offset(),
        ten2.size(),
        ten2.stride(),
    )




# Remote tensors are now handled directly by the C++ allocator with ID-based allocation


_remote_lib = torch.library.Library("_", "IMPL")
_remote_lib.fallback(_remote_kernel_fallback, dispatch_key=PRIVATEUSE1_DISPATCH_KEY)

_remote_lib_aten = torch.library.Library("aten", "IMPL")
_remote_lib_aten.impl("_copy_from", _copy_from, dispatch_key=PRIVATEUSE1_DISPATCH_KEY)
_remote_lib_aten.impl("_to_copy", _to_copy, dispatch_key=PRIVATEUSE1_DISPATCH_KEY)
_remote_lib_aten.impl(
    "set_.source_Tensor", _set_source_tensor, dispatch_key=PRIVATEUSE1_DISPATCH_KEY
)

# via TORCH_LIBRARY_IMPL in RemoteMem.cpp, so we don't register Python implementations

# These factory functions are now handled by C++ implementations
# via the registered TORCH_LIBRARY_IMPL dispatch system

# when we add them to TORCH_LIBRARY_IMPL in RemoteMem.cpp


def cleanup_library_registrations() -> None:
    """Clean up library registrations to prevent hanging."""
    global _remote_lib, _remote_lib_aten
    try:
        # PyTorch doesn't provide a clean way to unregister, but we can try
        # Calling this during cleanup might help
        if hasattr(_remote_lib, "_destroy"):
            _remote_lib._destroy()
        if hasattr(_remote_lib_aten, "_destroy"):
            _remote_lib_aten._destroy()
    except Exception:
        pass

