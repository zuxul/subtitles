import sys
from pathlib import Path

from audio_separator.separator import Separator


def main() -> int:
    if len(sys.argv) != 3:
        print("Использование script.py <входной.wav> <выходной.wav>")
        return 1

    input_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()

    if not input_path.is_file():
        print(f"Входной файл не найден: {input_path}")
        return 1
    if output_path.suffix.lower() != ".wav":
        print("Выходной файл должен иметь расширение .wav")
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Модель скачается автоматически при первом запуске.
    # output_single_stem: на диск пишется только речь, файл с музыкой не создаётся.
    separator = Separator(
        output_dir=str(output_path.parent),
        output_single_stem="Vocals",
    )
    separator.load_model("UVR-MDX-NET-Inst_HQ_4.onnx")

    output_files = separator.separate(
        str(input_path),
        custom_output_names={"Vocals": output_path.stem},
    )
    if not output_files:
        print("Не удалось выделить речь")
        return 1

    speech_file = Path(output_files[0])
    if not speech_file.is_absolute():
        speech_file = output_path.parent / speech_file
    if speech_file != output_path:
        speech_file.replace(output_path)

    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
