"""
Сервис для работы с WhisperX через API методы
"""

import os
import json
import tempfile
import subprocess
from pathlib import Path
from app.config import settings
import whisperx
import torch

import warnings

# Отключаем предупреждения
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*torchaudio._backend.list_audio_backends.*")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=".*whisperx.*")
warnings.filterwarnings("ignore", message=".*Lightning automatically upgraded.*")
warnings.filterwarnings("ignore", module="pytorch_lightning")
warnings.filterwarnings("ignore", module="speechbrain")
warnings.filterwarnings("ignore", module="transformers")


class WhisperXService:
    """Сервис для работы с WhisperX через API методы"""

    # Глобальный кэш для моделей whisperx
    _models_cache = {}
    _align_models_cache = {}

    def __init__(self, model_size: str = None, device: str = None):
        """
        Инициализация сервиса WhisperX
        
        Args:
            model_size: Размер модели (tiny, base, small, medium, large). По умолчанию из config
            device: Устройство (cuda или cpu). По умолчанию определяется автоматически
        """
        self.model_size = model_size or settings.whisperx_model
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.compute_type = "float16" if self.device == "cuda" else "int8"
        
        # Загружаем модель при инициализации
        self.model = self._load_model()
        
        if self.device == "cuda":
            print(f"Используем GPU: {torch.cuda.get_device_name(0)}")
            print(f"CUDA версия: {torch.version.cuda}")
            print(f"Compute type: {self.compute_type} (FP16)")
            torch.backends.cudnn.benchmark = True
        else:
            print("GPU не доступен, используем CPU")
            print(f"Compute type: {self.compute_type} (int8)")

    def _load_model(self):
        """Загружает модель WhisperX с кэшированием"""
        cache_key = f"{self.model_size}_{self.device}_{self.compute_type}"
        
        if cache_key not in self._models_cache:
            print(f"Загружаем модель whisperx: {self.model_size} на устройстве: {self.device}")
            model = whisperx.load_model(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type
            )
            self._models_cache[cache_key] = model
            print(f"✅ Модель whisperx загружена и закэширована")
        else:
            print(f"✅ Используем закэшированную модель whisperx: {self.model_size} на {self.device}")
            model = self._models_cache[cache_key]
        
        return model

    def _load_align_model(self, language_code: str):
        """Загружает модель выравнивания с кэшированием"""
        cache_key = f"align_{language_code}_{self.device}"
        
        if cache_key not in self._align_models_cache:
            print(f"Загружаем модель выравнивания для языка: {language_code}")
            align_model, metadata = whisperx.load_align_model(
                language_code=language_code,
                device=self.device
            )
            self._align_models_cache[cache_key] = (align_model, metadata)
            print(f"✅ Модель выравнивания загружена и закэширована")
        else:
            print(f"✅ Используем закэшированную модель выравнивания для языка: {language_code}")
            align_model, metadata = self._align_models_cache[cache_key]
        
        return align_model, metadata

    def transcribe_audio(self, audio_path: str):
        """
        Основной метод для транскрипции аудио
        Принимает audio_path и возвращает полный результат
        
        Args:
            audio_path: Путь к аудио файлу
            
        Returns:
            list: Список сегментов с транскрипцией
        """
        print(f"🎤 Начинаем транскрипцию аудио: {audio_path}")

        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудио файл не найден: {audio_path}")

        # Проверяем размер файла
        file_size = self.check_file_size(audio_path)
        print(f"  Размер файла: {file_size:.1f} МБ")

        # Проверяем, что файл не пустой
        if file_size < 0.1:  # Меньше 100KB
            raise ValueError(f"Аудио файл слишком мал ({file_size:.1f} МБ). Возможно, файл поврежден или пуст")

        if self.needs_chunking(file_size):
            print("  ⚠️ Файл слишком большой, разбиваем на части...")
            segments = self._transcribe_large_audio(audio_path)
        else:
            print("  ✅ Обрабатываем как один файл")
            segments = self._transcribe_single_audio(audio_path)

        print(f"✅ Транскрипция завершена: {len(segments)} сегментов")
        return segments

    def _transcribe_single_audio(self, audio_path: str):
        """Транскрибирует один аудиофайл"""
        print(f"🎤 Транскрибируем файл: {audio_path}")

        segments = self.transcribe_file(audio_path)

        print(f"✅ Транскрипция завершена: {len(segments)} сегментов")
        return segments

    def _transcribe_large_audio(self, audio_path: str):
        """Транскрибирует большой аудиофайл по частям"""
        print(f"🎤 Транскрибируем большой файл по частям: {audio_path}")

        # Получаем информацию об аудио
        try:
            from pydub import AudioSegment
        except ImportError:
            raise ImportError("pydub не установлен. Установите: pip install pydub")

        # Определяем формат файла по расширению
        file_ext = os.path.splitext(audio_path)[1].lower()
        if file_ext == '.mp3':
            audio = AudioSegment.from_mp3(audio_path)
        elif file_ext == '.wav':
            audio = AudioSegment.from_wav(audio_path)
        else:
            # Используем универсальный метод для других форматов
            audio = AudioSegment.from_file(audio_path)
        
        total_duration = len(audio) / 1000.0  # конвертируем в секунды

        chunk_duration = settings.chunk_duration_minutes * 60  # в секундах
        all_segments = []
        time_offset = 0

        for i in range(0, int(total_duration), int(chunk_duration)):
            start_time = i
            end_time = min(i + chunk_duration, total_duration)

            # Создаём временный чанк аудио
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_chunk:
                chunk_path = temp_chunk.name

            cmd_chunk = [
                "ffmpeg", "-y", "-i", audio_path,
                "-ss", str(start_time), "-t", str(end_time - start_time),
                "-c", "copy", chunk_path
            ]
            subprocess.run(cmd_chunk, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            print(f"  Обрабатываем чанк {i//int(chunk_duration) + 1}...")

            # Транскрибируем чанк
            segments = self.transcribe_file(chunk_path, time_offset)

            all_segments.extend(segments)
            time_offset += chunk_duration

            # Удаляем временный чанк
            if os.path.exists(chunk_path):
                os.remove(chunk_path)

        print(f"✅ Транскрипция большого файла завершена: {len(all_segments)} сегментов")
        return all_segments

    def transcribe_file(self, audio_path: str, time_offset: float = 0):
        """
        Транскрибирует аудиофайл с помощью WhisperX API и возвращает сегменты
        
        Args:
            audio_path: Путь к аудио файлу
            time_offset: Смещение времени для чанков (используется при разбиении больших файлов)
            
        Returns:
            list: Список сегментов с транскрипцией
        """
        print(f"🎤 WhisperX: транскрибируем {os.path.basename(audio_path)}")
        print(f"  Устройство: {self.device}")

        # Загружаем аудио
        audio = whisperx.load_audio(audio_path)

        # Транскрибируем аудио
        print("🔧 Выполняем транскрипцию...")
        result = self.model.transcribe(audio, batch_size=16)

        detected_language = result.get("language", "unknown")
        print(f"Транскрибация завершена. Язык: {detected_language}")

        # Выравниваем на уровне предложений
        print("Выполняем выравнивание на уровне предложений...")
        align_model, metadata = self._load_align_model(language_code=detected_language)

        # Выравниваем с align_sentences=True
        result = whisperx.align(
            result["segments"],
            align_model,
            metadata,
            audio,
            self.device,
            return_char_alignments=False,
            align_sentences=True
        )

        print("✅ Выравнивание завершено")

        # Обрабатываем сегменты
        segments = self.process_segments(result, time_offset)

        print(f"✅ Транскрипция завершена: {len(segments)} сегментов")
        return segments

    def process_segments(self, result: dict, time_offset: float = 0):
        """
        Обрабатывает сегменты из результата WhisperX без добавления сегментов тишины
        
        Args:
            result: Результат от whisperx.align() (словарь с ключом "segments")
            time_offset: Смещение времени для чанков
            
        Returns:
            list: Список обработанных сегментов
        """
        print(f"📝 Обрабатываем сегменты...")

        try:
            raw_segments = result.get("segments", [])
            print(f"  - Всего сегментов в файле: {len(raw_segments)}")

            if len(raw_segments) == 0:
                print(f"⚠️ WhisperX не нашел ни одного сегмента в аудио файле")
                print(f"  Возможные причины:")
                print(f"  - Аудио слишком тихое")
                print(f"  - Только фоновый шум без речи")
                print(f"  - Поврежденный аудио файл")
                print(f"  - Неподдерживаемый формат аудио")
                return []

            segments = []
            empty_segments = 0

            # Обрабатываем только сегменты с текстом (без добавления тишины)
            for i, segment in enumerate(raw_segments):
                text = segment.get("text", "").strip()
                if text:
                    segment_dict = {
                        "start": round(segment.get("start", 0) + time_offset, 3),
                        "end": round(segment.get("end", 0) + time_offset, 3),
                        "text": text
                    }
                    segments.append(segment_dict)
                else:
                    empty_segments += 1
                    if i < 5:  # Показываем первые 5 пустых сегментов для диагностики
                        print(f"  - Сегмент {i+1}: пустой текст '{segment.get('text', '')}'")

            if empty_segments > 0:
                print(f"  - Пустых сегментов: {empty_segments}")

            print(f"✅ Обработано {len(segments)} сегментов с голосом (без тишины)")
            return segments

        except Exception as e:
            print(f"❌ Ошибка обработки сегментов: {e}")
            raise

    def check_file_size(self, audio_path: str) -> float:
        """
        Проверяет размер файла в МБ
        
        Args:
            audio_path: Путь к файлу
            
        Returns:
            float: Размер файла в МБ
        """
        file_size = os.path.getsize(audio_path) / (1024 * 1024)  # в МБ
        return file_size

    def needs_chunking(self, file_size: float) -> bool:
        """
        Проверяет, нужно ли разбивать файл на части
        
        Args:
            file_size: Размер файла в МБ
            
        Returns:
            bool: True если файл нужно разбивать на части
        """
        # WhisperX может обрабатывать большие файлы, но для стабильности ограничиваем
        return file_size > 100  # 100 МБ

