ml pytorch/2.8.0
export ALIGNMENT_VENV="$PSCRATCH/venvs/assignment5-alignment"
export UV_PROJECT_ENVIRONMENT="$ALIGNMENT_VENV"
export UV_CACHE_DIR="$PSCRATCH/.cache/uv"
export HF_HOME="$PSCRATCH/.cache/huggingface"
export VLLM_CACHE_ROOT="$PSCRATCH/.cache/vllm-0.19.1"
export HF_XET_HIGH_PERFORMANCE=1
export WANDB_API_KEY=$(cat ~/wandb_key)

seed=42
mkdir -p logs
uv run --locked --exact --extra gpu python -m cs336_alignment.GRPO \
    --gradient_accumulation_steps 16 \
    --log_eval_rollouts \
    --seed ${seed} \
    |& tee ./logs/seed_${seed}.log