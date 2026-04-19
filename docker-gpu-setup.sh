#!/bin/bash
# MNEMOS-OS Docker GPU Setup Helper
# Detects and configures GPU support for Linux, macOS, and Windows (WSL2)
#
# Usage:
#   ./docker-gpu-setup.sh          # Auto-detect and print configuration
#   ./docker-gpu-setup.sh --build   # Build Docker image with detected GPU
#   ./docker-gpu-setup.sh --test    # Test GPU availability
#
# Supports:
#   - Linux: NVIDIA CUDA, AMD ROCm, Intel Arc
#   - macOS: Metal (Apple Silicon)
#   - Windows (WSL2): NVIDIA CUDA (via WSL), AMD ROCm

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_TYPE="$(uname -s)"
DETECTED_GPU="none"
GPU_RUNTIME="runc"
DOCKERFILE_GPU_ARGS=""

# ────────────────────────────────────────────────────────────────────────────
# Detection Functions
# ────────────────────────────────────────────────────────────────────────────

detect_linux_gpu() {
    # Check for NVIDIA CUDA
    if command -v nvidia-smi &>/dev/null; then
        local cuda_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)
        echo "✓ NVIDIA CUDA detected (driver version: $cuda_version)"
        DETECTED_GPU="nvidia"
        GPU_RUNTIME="nvidia"
        return 0
    fi

    # Check for AMD ROCm
    if command -v rocm-smi &>/dev/null; then
        echo "✓ AMD ROCm detected"
        DETECTED_GPU="rocm"
        GPU_RUNTIME="runc"  # ROCm uses standard Docker runtime
        return 0
    fi

    # Check for Intel Arc
    if lspci | grep -i "intel.*arc" &>/dev/null; then
        echo "✓ Intel Arc detected"
        DETECTED_GPU="intel-arc"
        GPU_RUNTIME="runc"
        return 0
    fi

    echo "✗ No GPU detected on Linux (falling back to CPU)"
    DETECTED_GPU="cpu"
    return 1
}

detect_macos_gpu() {
    # macOS: Check for Apple Silicon (Metal) or Intel GPU
    local cpu_brand=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "unknown")

    if [[ "$cpu_brand" == *"Apple"* ]]; then
        echo "✓ Apple Silicon (Metal) detected"
        DETECTED_GPU="metal"
        GPU_RUNTIME="runc"
        return 0
    elif [[ "$cpu_brand" == *"Intel"* ]]; then
        echo "✓ Intel GPU (macOS) detected (via Metal compatibility)"
        DETECTED_GPU="metal"
        GPU_RUNTIME="runc"
        return 0
    fi

    echo "✗ No GPU detected on macOS"
    DETECTED_GPU="cpu"
    return 1
}

detect_wsl2_gpu() {
    # Windows WSL2: Check for NVIDIA CUDA via WSL
    if command -v nvidia-smi &>/dev/null; then
        local cuda_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1)
        if [ -n "$cuda_version" ]; then
            echo "✓ NVIDIA CUDA in WSL2 detected (driver: $cuda_version)"
            DETECTED_GPU="nvidia-wsl2"
            GPU_RUNTIME="nvidia"
            return 0
        fi
    fi

    # Check for AMD ROCm in WSL2
    if command -v rocm-smi &>/dev/null; then
        echo "✓ AMD ROCm in WSL2 detected"
        DETECTED_GPU="rocm-wsl2"
        GPU_RUNTIME="runc"
        return 0
    fi

    echo "✗ No GPU detected in WSL2 (falling back to CPU)"
    DETECTED_GPU="cpu-wsl2"
    return 1
}

# ────────────────────────────────────────────────────────────────────────────
# Main Detection
# ────────────────────────────────────────────────────────────────────────────

detect_gpu() {
    case "$OS_TYPE" in
        Linux)
            echo "[Linux] Detecting GPU..."
            detect_linux_gpu || true
            ;;
        Darwin)
            echo "[macOS] Detecting GPU..."
            detect_macos_gpu || true
            ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "[Windows/WSL] Detecting GPU..."
            detect_wsl2_gpu || true
            ;;
        *)
            echo "✗ Unknown OS: $OS_TYPE"
            DETECTED_GPU="cpu"
            ;;
    esac
}

# ────────────────────────────────────────────────────────────────────────────
# Docker Configuration
# ────────────────────────────────────────────────────────────────────────────

generate_docker_args() {
    case "$DETECTED_GPU" in
        nvidia)
            # Linux with NVIDIA
            DOCKERFILE_GPU_ARGS="--build-arg CUDA_BASE=nvidia/cuda:12.2.2-runtime-ubuntu22.04"
            echo "docker run --gpus all -it mnemos:latest"
            ;;
        nvidia-wsl2)
            # WSL2 with NVIDIA
            DOCKERFILE_GPU_ARGS="--build-arg CUDA_BASE=nvidia/cuda:12.2.2-runtime-ubuntu22.04"
            echo "docker run --gpus all -it mnemos:latest  # (WSL2 passes through automatically)"
            ;;
        rocm)
            # Linux with AMD ROCm
            DOCKERFILE_GPU_ARGS="--build-arg ROCM_BASE=rocm/dev-ubuntu-22.04:5.7"
            echo "docker run --device=/dev/kfd --device=/dev/dri -it mnemos:latest"
            ;;
        rocm-wsl2)
            # WSL2 with AMD ROCm
            DOCKERFILE_GPU_ARGS="--build-arg ROCM_BASE=rocm/dev-ubuntu-22.04:5.7"
            echo "docker run --device=/dev/kfd --device=/dev/dri -it mnemos:latest"
            ;;
        metal)
            # macOS (Metal via Docker Desktop)
            echo "docker run -it mnemos:latest  # (Metal GPU support built-in to Docker Desktop)"
            ;;
        intel-arc)
            # Intel Arc (Linux)
            echo "docker run --device=/dev/dri -it mnemos:latest"
            ;;
        *)
            # CPU fallback
            echo "docker run -it mnemos:latest"
            ;;
    esac
}

# ────────────────────────────────────────────────────────────────────────────
# Commands
# ────────────────────────────────────────────────────────────────────────────

cmd_detect() {
    detect_gpu
    echo ""
    echo "Summary:"
    echo "  OS Type: $OS_TYPE"
    echo "  Detected GPU: $DETECTED_GPU"
    echo "  Docker Runtime: $GPU_RUNTIME"
    echo ""
    echo "Recommended Docker run command:"
    generate_docker_args
}

cmd_build() {
    detect_gpu
    echo "Building MNEMOS image with $DETECTED_GPU support..."

    case "$DETECTED_GPU" in
        nvidia|nvidia-wsl2)
            docker build $DOCKERFILE_GPU_ARGS \
                --build-arg BASE_IMAGE=nvidia/cuda:12.2.2-runtime-ubuntu22.04 \
                -t mnemos:latest .
            ;;
        rocm|rocm-wsl2)
            docker build $DOCKERFILE_GPU_ARGS \
                --build-arg BASE_IMAGE=rocm/dev-ubuntu-22.04:5.7 \
                -t mnemos:latest .
            ;;
        metal|intel-arc|cpu|cpu-wsl2)
            docker build -t mnemos:latest .
            ;;
    esac
}

cmd_test() {
    detect_gpu
    echo "Testing GPU availability in Docker..."

    case "$DETECTED_GPU" in
        nvidia|nvidia-wsl2)
            docker run --rm --gpus all python:3.11 nvidia-smi
            ;;
        rocm|rocm-wsl2)
            docker run --rm --device=/dev/kfd --device=/dev/dri rocm/dev-ubuntu-22.04:5.7 rocm-smi
            ;;
        *)
            echo "No dedicated GPU test available for $DETECTED_GPU"
            echo "Running basic Python test..."
            docker run --rm python:3.11 python -c "import sys; print(f'Python {sys.version}')"
            ;;
    esac
}

# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

case "${1:-detect}" in
    detect)
        cmd_detect
        ;;
    --build|build)
        cmd_build
        ;;
    --test|test)
        cmd_test
        ;;
    --help|help|-h)
        echo "MNEMOS-OS Docker GPU Setup Helper"
        echo ""
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  detect (default)  - Detect GPU and print configuration"
        echo "  build --build     - Build Docker image with detected GPU support"
        echo "  test  --test      - Test GPU availability in Docker"
        echo "  help  -h/--help   - Show this help"
        ;;
    *)
        echo "Unknown command: $1"
        echo "Use '$0 --help' for usage information"
        exit 1
        ;;
esac
