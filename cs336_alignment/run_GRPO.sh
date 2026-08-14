ml pytorch/2.8.0
export ALIGNMENT_VENV="$PSCRATCH/venvs/assignment5-alignment"
export UV_PROJECT_ENVIRONMENT="$ALIGNMENT_VENV"
export UV_CACHE_DIR="$PSCRATCH/.cache/uv"
export HF_HOME="$PSCRATCH/.cache/huggingface"
export VLLM_CACHE_ROOT="$PSCRATCH/.cache/vllm-0.19.1"
export HF_XET_HIGH_PERFORMANCE=1
uv run --locked --exact --extra gpu python -m cs336_alignment.GRPO 