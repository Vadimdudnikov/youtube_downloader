#!/usr/bin/env python3
"""
Wrapper для WhisperX CLI с патчем для PyTorch 2.6+
"""
import sys
import torch
import omegaconf

# Явно разрешаем загрузку Pyannote-чекпоинтов
# Это необходимо для работы WhisperX с PyTorch 2.6+
torch.serialization.add_safe_globals([omegaconf.listconfig.ListConfig])

# Патчим torch.load для отключения weights_only
_original_torch_load = torch.load

def _patched_torch_load(*args, **kwargs):
    """Патч для torch.load с отключением weights_only для совместимости"""
    kwargs['weights_only'] = False
    return _original_torch_load(*args, **kwargs)

torch.load = _patched_torch_load

# Также патчим torch.load в lightning_fabric перед импортом whisperx
# Это нужно для загрузки Pyannote моделей через lightning
try:
    import lightning_fabric.utilities.cloud_io as cloud_io
    if hasattr(cloud_io, '_load'):
        original_pl_load = cloud_io._load
        def patched_pl_load(*args, **kwargs):
            kwargs['weights_only'] = False
            return original_pl_load(*args, **kwargs)
        cloud_io._load = patched_pl_load
except Exception as e:
    print(f"Warning: Could not patch lightning_fabric.cloud_io: {e}", file=sys.stderr)

# Импортируем и запускаем whisperx CLI
from whisperx import __main__

if __name__ == "__main__":
    __main__.cli()

