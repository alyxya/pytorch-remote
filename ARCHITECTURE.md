# torch-remote Architecture Documentation

This document provides a detailed explanation of the torch-remote codebase architecture, including the purpose and functionality of each source file.

## Overview

torch-remote implements a custom PyTorch device that automatically routes computations to remote cloud providers. The system uses a three-layer architecture with a pluggable multi-provider backend system. Modal serves as the first supported provider, with additional providers planned for future releases. The architecture includes private package isolation to prevent provider-specific import conflicts.

## Architecture Layers

### 1. PyTorch Integration Layer (C++ Extension)
Registers "remote" as a native PyTorch PrivateUse1 device with full integration into PyTorch's dispatch system.

### 2. Local Device Simulation Layer (Python)
Manages remote tensors using CPU memory with threading-based device simulation for local operations.

### 3. Remote Execution Layer (Multi-Provider)
Automatically dispatches compute-intensive operations to cloud providers via a pluggable backend system. Currently supports Modal's A100 GPU infrastructure, with additional providers planned.

## Package Structure

```
torch_remote/                   # Main PyTorch extension package
├── __init__.py                 # Package initialization & device registration
├── _aten_impl.py              # Operation dispatch & remote execution logic  
├── _remote_executor.py        # Remote execution system
├── _meta_parser.py            # Tensor metadata & data structures
├── _device_daemon.py          # Device management & process communication
├── device.py                  # Backend device abstraction & registry
├── utils.py                   # Tensor method extensions
├── backends/                  # Multi-provider backend system
│   ├── __init__.py            # Backend registry & management
│   └── modal/                 # Modal provider implementation
│       └── __init__.py        # Modal backend integration
└── csrc/                      # C++ extension
    ├── remote_extension.cpp   # Python extension entry point
    ├── RemoteHooks.cpp        # PrivateUse1 backend implementation
    ├── RemoteMem.cpp          # Memory management
    └── Remote.h               # C++ header definitions

torch_remote_execution/        # Private package for remote execution
├── __init__.py               # Private package marker
├── modal_app.py              # Modal multi-GPU execution app (T4, L4, A10G, A100-40GB, A100-80GB, L40S, H100, H200, B200)
└── setup.py                  # Private package installation
```

## Source File Details

### torch_remote Package (Main Package)

#### Core Module Files

**`torch_remote/__init__.py`** - Package Initialization & Device Registration
- Registers "remote" as a PyTorch PrivateUse1 backend device
- Creates the `torch.remote` module with device management functions:
  - `device()` - Context manager for device selection
  - `device_count()` - Number of available remote devices
  - `current_device()` - Get current device index
  - `is_available()` - Check device availability
- Sets up random number generation, streams, and device context management
- Imports and initializes the C++ extension

**`torch_remote/_aten_impl.py`** - Operation Dispatch & Remote Execution Logic
- **Primary dispatch system**: Handles all ATen operations on remote tensors
- **Remote execution decision logic**: `_should_use_remote_execution()` determines which operations should run on cloud providers vs locally
- **Operation filtering**: 
  - Skip lists for memory ops (copy_, resize_, etc.)
  - Factory functions (empty, zeros, ones)
  - View operations (reshape, transpose, etc.) that should stay local
- **Compute-intensive operation routing**: Automatically routes to remote cloud providers:
  - Matrix operations (mm, bmm, addmm)
  - Neural network operations (conv2d, linear, relu, softmax)
  - Reduction operations (sum, mean, var, std)
  - Large tensor operations (>1000 elements)
- **Fallback mechanisms**: Local execution when remote is unavailable
- **Factory function support**: Handles tensor creation operations on remote device
- **Library registration**: Registers remote device implementations for specific PyTorch operations

**`torch_remote/_remote_executor.py`** - Remote Execution System
- **RemoteExecutor class**: Manages remote execution across cloud providers using stateful RemoteGPUMachine instances
- **Tensor serialization/deserialization**: Converts remote tensors to/from bytes for network transport
- **Provider backend integration**: Interfaces with the `torch_remote_execution` package
- **Error handling and fallbacks**: Graceful degradation when remote providers are unavailable
- **Device validation**: Enforces single-device operations and prevents mixed-device tensor operations
- **Device conversion helpers**: Converts between remote tensors and CPU tensors for transport
- **Stateful execution**: Manages device-specific GPU machines with caching for improved performance

**`torch_remote/_meta_parser.py`** - Tensor Metadata & Data Structures
- **RemoteTensorMeta class**: Captures tensor metadata (shape, dtype, strides, storage info) for serialization
- **RemoteTensorData class**: Custom tensor subclass that:
  - Reports "remote" device but stores data on CPU
  - Overrides `.device` property to return `torch.device("remote", index)`
  - Provides proper `.cpu()` method that returns regular torch.Tensor
- **Serialization helpers**: Convert tensors to/from metadata for inter-process communication
- **Device spoofing**: Makes CPU tensors appear as remote device tensors to PyTorch
- **Validation functions**: Ensures only valid data types pass through device boundaries

**`torch_remote/_device_daemon.py`** - Device Management & Process Communication
- **Driver class**: Main coordinator for device operations and memory management
- **Threading-based execution**: Uses Python threads instead of multiprocessing to avoid hanging issues
- **Memory allocators**: 
  - `Allocator` - Base allocator class with malloc/free interface
  - `HostAllocator` - Manages pinned host memory
  - `DeviceAllocator` - Manages device memory and tensor reconstruction
- **Device simulation**: Simulates 2 remote devices using CPU memory
- **Stream and event management**: PyTorch CUDA-like stream semantics for remote device
- **Cleanup handling**: Signal handlers and atexit hooks for proper resource cleanup
- **Operation execution**: Routes operations to worker threads via queue-based communication
- **_Executor class**: Worker thread that actually performs tensor operations

#### Utility Files

**`torch_remote/utils.py`** - Tensor Method Extensions
- Patches `.to()` method to support `BackendDevice` objects
- Enables `tensor.to(backend_device)` to move tensors to remote device
- Simple wrapper around the C++ remote conversion function

**`torch_remote/device.py`** - Backend Device Abstraction & Registry
- **BackendDevice class**: Represents a remote GPU device with specific provider and GPU type
  - Unique device ID generation with provider-gpu-uuid format
  - GPU type validation for provider compatibility
  - Device equality and hashing based on unique device ID
  - Provider-specific configuration support
- **DeviceRegistry class**: Manages active BackendDevice instances
  - Device registration with automatic index assignment
  - Device lookup by ID or index
  - Device compatibility validation for operations
  - Enforces single-device constraint for tensor operations
- **GPUType enum**: Supported GPU types (T4, L4, A10G, A100-40GB, A100-80GB, L40S, H100, H200, B200)
- **BackendProvider enum**: Supported cloud providers (Modal, with future providers planned)
- **Factory functions**: `create_modal_device()` for easy Modal device creation
- **Global registry**: Shared device registry for system-wide device management

#### Backend System Files

**`torch_remote/backends/__init__.py`** - Backend Registry & Management
- **Backend registry**: Manages available cloud provider backends
- **Provider interface**: Defines the standard interface all backends must implement
- **Backend loading**: Dynamically loads and initializes provider backends
- **Configuration management**: Handles provider-specific configuration and credentials
- **Fallback logic**: Manages fallback between providers when one is unavailable

**`torch_remote/backends/modal/__init__.py`** - Modal Backend Integration
- **Modal backend implementation**: Implements the standard provider interface for Modal
- **Authentication handling**: Manages Modal API tokens and authentication
- **Resource configuration**: Handles Modal-specific GPU and container configurations
- **Error translation**: Converts Modal-specific errors to standard backend errors


#### C++ Extension

**`torch_remote/csrc/remote_extension.cpp`** - Python Extension Entry Point
- PyTorch C++ extension initialization using PyBind11
- Exposes `_init()` function to initialize PrivateUse1 device
- Provides `_get_default_generator()` for random number generation
- Links Python factory functions to C++ implementation
- Sets up the bridge between Python and C++ components

**`torch_remote/csrc/RemoteHooks.cpp`** - PrivateUse1 Backend Implementation
- Implements PyTorch's `PrivateUse1HooksInterface` for full device integration
- **Device management**: device count, current device, device guard implementation
- **Generator management**: Random number generators for remote device
- **Stream management**: Stream creation, synchronization, and querying
- **Memory management**: Host allocator integration
- **Event system**: Event creation, recording, and synchronization
- Integrates with Python-based device driver through method calls

**`torch_remote/csrc/RemoteMem.cpp`** - Memory Management
- **RemoteAllocator class**: Handles device memory allocation/deallocation
- Integrates with Python-based memory management system
- Routes allocation requests through Python driver
- Registers allocator with PyTorch's memory management system
- Handles memory cleanup and error reporting

**`torch_remote/csrc/Remote.h`** - C++ Header Definitions
- Common types and utilities for the C++ extension
- `remote_ptr_t` - Pointer type for remote device memory
- Python GIL management helpers
- Template functions for cleanup and error reporting
- Method lookup utilities for calling Python functions from C++

### torch_remote_execution Package (Private Package)

**`torch_remote_execution/__init__.py`** - Private Package Marker
- Simple package initialization with version information
- Documentation warning against direct use
- Marks package as internal to torch_remote

**`torch_remote_execution/modal_app.py`** - Modal Multi-GPU Execution App
- **RemoteGPUMachine class**: Stateful wrapper representing a remote GPU machine running on Modal
  - Encapsulates Modal app and executor with connection management
  - Context manager support for automatic resource cleanup
  - Device-specific initialization and state management
  - Machine lifecycle operations (start, stop, is_running)
- **Modal application definition**: Creates device-specific Modal apps with unique identifiers
- **Docker image setup**: 
  - Debian slim base with Python 3.11
  - Installs PyTorch with CUDA support
- **Multi-GPU support**: Dynamic creation of device-specific execution functions for each GPU type:
  - T4, L4, A10G, A100-40GB, A100-80GB, L40S, H100, H200, B200
- **Device-specific GPU routing**: Each machine configured with appropriate GPU type, timeout, and retry settings
- **Stateful execution model**: 
  - `create_modal_app_for_gpu()` creates device-specific RemoteGPUMachine instances
  - Machine caching prevents redundant app creation for same device
  - Context management ensures proper resource cleanup
- **Common execution implementation**: Shared execution logic that:
  - Receives serialized tensors, metadata, args, and kwargs
  - Deserializes tensors and moves them to CUDA device
  - Processes tensor placeholders in arguments
  - Executes the requested ATen operation on specified GPU
  - Serializes results and returns them
- **GPU utilization**: Automatically detects and uses CUDA when available
- **Error handling**: Comprehensive error reporting and traceback printing
- **Dynamic app creation**: Creates unique Modal apps per device to support multiple concurrent GPU machines

**`torch_remote_execution/setup.py`** - Private Package Installation
- Main setuptools configuration for the remote execution package
- Dependencies: modal>=0.60.0, torch>=2.0.0
- Marked as development status to discourage standalone installation
- Classifiers indicate it's for internal use

## Operation Flow

Here's how a typical operation flows through the system:

1. **User Code**: `result = torch.add(remote_tensor_a, remote_tensor_b)`

2. **PyTorch Dispatch**: PyTorch's dispatch system routes to remote device implementation

3. **_aten_impl.py**: 
   - `_remote_kernel_fallback` or `_kernel_fallback` receives the operation
   - `_should_use_remote_execution()` decides if this should run remotely
   - **Device validation**: Ensures all tensors belong to same device before operation
   - For compute-intensive ops: routes to remote execution
   - For simple ops: handles locally

4. **Remote Execution Path** (if enabled):
   - `RemoteExecutor.execute_remote_operation()` is called
   - **Device detection and validation**: `_detect_device_from_tensors()` ensures single-device operation
   - Device-specific RemoteGPUMachine is retrieved or created
   - Tensors are serialized to bytes
   - RemoteGPUMachine context is started (Modal app)
   - Appropriate GPU-specific function is called based on device configuration
   - `PytorchOperationExecutor.execute_aten_operation()` runs on cloud GPU
   - Results are serialized and returned
   - Results are deserialized back to remote tensors with original device ID preserved

5. **Local Execution Path** (fallback):
   - Operation metadata is computed
   - Output tensors are allocated on remote device
   - Operation is executed via device daemon
   - Results are returned as remote tensors

## Key Design Decisions

### Private Package Isolation
The most important architectural decision is separating the provider backend code into `torch_remote_execution`. This prevents import conflicts when cloud provider jobs execute, since providers would otherwise try to import the entire `torch_remote` extension and create circular dependencies. The execution package is isolated with minimal dependencies.

### Threading vs Multiprocessing
The system uses threading instead of multiprocessing for device simulation to avoid complex cleanup issues that were causing hanging processes.

### Lazy Remote Execution
Remote execution is lazy-loaded and gracefully degrades when cloud providers are not available, allowing the extension to work in environments without specific providers. The multi-provider system allows fallback between different backends.

### Operation Filtering
Smart filtering ensures that only operations that benefit from cloud GPU acceleration are sent remotely, while keeping memory operations, views, and small tensor operations local for efficiency.

### Stateful Remote Execution
The system uses stateful RemoteGPUMachine instances for improved performance and resource management:
- **Device-specific machines**: Each BackendDevice gets its own RemoteGPUMachine instance
- **Connection caching**: Modal app contexts are reused across operations on the same device
- **Context management**: Automatic startup/shutdown of remote GPU resources
- **Machine lifecycle**: Start, stop, and running state management for remote resources

### Multi-GPU Support
The system supports multiple GPU types through device-specific RemoteGPUMachine instances, allowing automatic routing to the most appropriate GPU based on workload requirements and availability.

### CPU Storage with Device Spoofing
Remote tensors are stored in CPU memory but report as "remote" device to PyTorch, enabling seamless integration with PyTorch's device system while maintaining compatibility.

### Device Validation and Isolation
The system enforces strict device isolation to prevent operations between tensors on different remote devices:
- **Single-device constraint**: Operations can only be performed between tensors on the same BackendDevice instance
- **Device ID tracking**: Each tensor maintains a `_device_id` attribute linking it to its specific remote device
- **Cross-device detection**: `_detect_device_from_tensors()` validates all tensors in an operation belong to the same device
- **Error prevention**: Mixed-device operations raise clear error messages before execution
- **Provider isolation**: Different cloud provider instances are treated as distinct devices

## Configuration

The system uses Modal as the default cloud provider backend.

## Testing

The system includes comprehensive testing:
- `test_torch_remote.py` - Unit tests for all functionality
- Provider-specific tests for backend validation
- Pytest-based test suite with cleanup handling

This architecture provides a seamless PyTorch device experience while leveraging cloud provider GPU infrastructure for high-performance computing. The multi-provider system with stateful execution, device validation, and backend abstraction allows users to choose their preferred cloud backend while maintaining a consistent API and ensuring safe, efficient operations across different remote GPU devices.