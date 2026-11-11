import argparse
import json
from math import gcd
from pathlib import Path
import torch
import soundfile as sf
from silero_vad import load_silero_vad, get_speech_timestamps


def load_audio_mono(path: str, sampling_rate: int = 16000) -> torch.Tensor:
    """Читает аудиофайл, приводит к моно и ресемплирует в sampling_rate."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.mean(axis=1))

    if sr != sampling_rate:
        try:
            from scipy.signal import resample_poly
            g = gcd(sr, sampling_rate)
            wav = torch.from_numpy(
                resample_poly(wav.numpy(), sampling_rate // g, sr // g).astype("float32")
            )
        except ImportError:
            # Запасной вариант без scipy: линейная интерполяция
            new_len = int(round(len(wav) * sampling_rate / sr))
            wav = torch.nn.functional.interpolate(
                wav.view(1, 1, -1), size=new_len, mode="linear", align_corners=False
            ).view(-1)

    return wav


def extract_speech_with_map(
    input_audio_path: str,
    output_audio_path: str,
    output_json_path: str,
    gap_sec: float = 0.3,
    sampling_rate: int = 16000,
    min_silence_duration_ms: int = 300,
    speech_pad_ms: int = 100
):
    # 1. Загрузка модели Silero VAD
    model = load_silero_vad()

    # 2. Чтение аудио: приводим к моно и ресемплируем в 16 кГц
    wav = load_audio_mono(input_audio_path, sampling_rate=sampling_rate)

    # 3. Поиск участков с речью (возвращает таймкоды в сэмплах)
    speech_timestamps = get_speech_timestamps(
        wav,
        model,
        sampling_rate=sampling_rate,
        min_silence_duration_ms=min_silence_duration_ms,
        speech_pad_ms=speech_pad_ms,
        return_seconds=False
    )

    if not speech_timestamps:
        print("Речь в аудиофайле не обнаружена.")
        return

    # Подготовка тензора тишины, который будет вставляться между кусками
    gap_samples = int(gap_sec * sampling_rate)
    silence_tensor = torch.zeros(gap_samples, dtype=wav.dtype)

    output_chunks = []
    mapping_data = []
    current_new_sample = 0

    # 4. Сборка нового аудио и расчет обратных таймкодов
    for idx, ts in enumerate(speech_timestamps):
        orig_start_sample = ts['start']
        orig_end_sample = ts['end']
        chunk = wav[orig_start_sample:orig_end_sample]
        chunk_len = len(chunk)

        # Добавляем паузу перед каждым куском, кроме самого первого
        if idx > 0 and gap_samples > 0:
            output_chunks.append(silence_tensor)
            current_new_sample += gap_samples

        new_start_sample = current_new_sample
        new_end_sample = current_new_sample + chunk_len

        output_chunks.append(chunk)
        current_new_sample = new_end_sample

        # Записываем соответствие координат в секундах и сэмплах
        mapping_data.append({
            "segment_id": idx,
            "orig_start_sec": round(orig_start_sample / sampling_rate, 4),
            "orig_end_sec": round(orig_end_sample / sampling_rate, 4),
            "new_start_sec": round(new_start_sample / sampling_rate, 4),
            "new_end_sec": round(new_end_sample / sampling_rate, 4),
            "orig_samples": [orig_start_sample, orig_end_sample],
            "new_samples": [new_start_sample, new_end_sample]
        })

    # 5. Склейка всех тензоров и сохранение аудио
    final_wav = torch.cat(output_chunks)
    sf.write(output_audio_path, final_wav.numpy(), sampling_rate)

    # 6. Сохранение метаданных и карты таймкодов в JSON
    result_metadata = {
        "source_file": str(Path(input_audio_path).name),
        "output_file": str(Path(output_audio_path).name),
        "sampling_rate": sampling_rate,
        "gap_between_chunks_sec": gap_sec,
        "total_segments": len(mapping_data),
        "segments": mapping_data
    }

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(result_metadata, f, ensure_ascii=False, indent=2)

    print(f"Готово! Найдено фрагментов: {len(mapping_data)}")
    print(f"Аудио сохранено в: {output_audio_path}")
    print(f"Карта таймкодов сохранена в: {output_json_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Вырезает участки с речью (Silero VAD) и строит карту таймкодов."
    )
    parser.add_argument("input_audio_path", help="Входной аудиофайл")
    parser.add_argument(
        "-o", "--output-audio",
        default=None,
        help="Куда сохранить аудио (по умолчанию <stem>_vad<ext>)",
    )
    parser.add_argument(
        "-j", "--output-json",
        default=None,
        help="Куда сохранить карту таймкодов (по умолчанию <stem>_vad_map.json)",
    )
    parser.add_argument("--gap-sec", type=float, default=0.4,
                        help="Длительность паузы между фрагментами в секундах (по умолчанию 0.4)")
    parser.add_argument("--sampling-rate", type=int, default=16000,
                        help="Частота дискретизации (по умолчанию 16000)")
    parser.add_argument("--min-silence-ms", type=int, default=400,
                        help="Минимальная длительность тишины в мс (по умолчанию 400)")
    parser.add_argument("--speech-pad-ms", type=int, default=100,
                        help="Отступ вокруг речи в мс (по умолчанию 100)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    src = Path(args.input_audio_path)
    output_audio_path = args.output_audio or str(src.with_name(f"{src.stem}_vad{src.suffix}"))
    output_json_path = args.output_json or str(src.with_name(f"{src.stem}_vad_map.json"))

    extract_speech_with_map(
        input_audio_path=args.input_audio_path,
        output_audio_path=output_audio_path,
        output_json_path=output_json_path,
        gap_sec=args.gap_sec,
        sampling_rate=args.sampling_rate,
        min_silence_duration_ms=args.min_silence_ms,
        speech_pad_ms=args.speech_pad_ms,
    )
