import argparse
import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path


TIME_RE = re.compile(
    r"^(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
    r"\s*-->\s*"
    r"(?P<h2>\d+):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{3})"
    r"(?P<tail>.*)$"
)


@dataclass
class Cue:
    index: str
    start: float
    end: float
    tail: str
    text: list[str]


def parse_timestamp(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(path: Path) -> list[Cue]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = re.split(r"\n[ \t]*\n", text.strip())
    cues: list[Cue] = []

    for block_number, block in enumerate(raw_blocks, start=1):
        lines = block.split("\n")
        timing_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_idx is None:
            raise ValueError(f"{path}: блок {block_number} не содержит таймкода")

        match = TIME_RE.match(lines[timing_idx].strip())
        if not match:
            raise ValueError(
                f"{path}: некорректный таймкод в блоке {block_number}: "
                f"{lines[timing_idx]!r}"
            )

        cues.append(
            Cue(
                index="\n".join(lines[:timing_idx]),
                start=parse_timestamp(
                    match["h"], match["m"], match["s"], match["ms"]
                ),
                end=parse_timestamp(
                    match["h2"], match["m2"], match["s2"], match["ms2"]
                ),
                tail=match["tail"],
                text=lines[timing_idx + 1:],
            )
        )

    return cues


class TimeMapper:
    """Переводит время из склеенного VAD-аудио обратно в исходное аудио."""

    def __init__(self, segments: list[dict], sampling_rate: float):
        points: list[tuple[float, float]] = []

        for segment in segments:
            new_samples = segment.get("new_samples")
            orig_samples = segment.get("orig_samples")
            if new_samples and orig_samples:
                new_start, new_end = (v / sampling_rate for v in new_samples)
                orig_start, orig_end = (v / sampling_rate for v in orig_samples)
            else:
                new_start = float(segment["new_start_sec"])
                new_end = float(segment["new_end_sec"])
                orig_start = float(segment["orig_start_sec"])
                orig_end = float(segment["orig_end_sec"])

            points.append((new_start, orig_start))
            points.append((new_end, orig_end))

        if not points:
            raise ValueError("В карте нет сегментов")

        points.sort()
        self.points = points
        self.new_times = [point[0] for point in points]

    def map(self, new_time: float) -> float:
        if new_time <= self.points[0][0]:
            return self.points[0][1]
        if new_time >= self.points[-1][0]:
            return self.points[-1][1]

        i = bisect_right(self.new_times, new_time) - 1
        new_start, orig_start = self.points[i]
        new_end, orig_end = self.points[i + 1]

        if new_end == new_start:
            return orig_end

        ratio = (new_time - new_start) / (new_end - new_start)
        return orig_start + ratio * (orig_end - orig_start)


def write_srt(path: Path, cues: list[Cue], mapper: TimeMapper) -> None:
    blocks: list[str] = []

    for cue in cues:
        start = mapper.map(cue.start)
        end = mapper.map(cue.end)
        if end <= start:
            end = start + 0.001

        lines: list[str] = []
        if cue.index:
            lines.append(cue.index)
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}{cue.tail}")
        lines.extend(cue.text)
        blocks.append("\n".join(lines))

    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def default_output_path(srt_path: Path) -> Path:
    if srt_path.stem.endswith("_vad"):
        return srt_path.with_name(f"{srt_path.stem[:-4]}{srt_path.suffix}")
    return srt_path.with_name(f"{srt_path.stem}_unmapped{srt_path.suffix}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Пересчитывает таймкоды SRT из склеенного VAD-аудио "
            "в исходную шкалу времени по карте *_vad_map.json."
        )
    )
    parser.add_argument("srt", help="SRT, распознанный по VAD-аудио")
    parser.add_argument("map", help="JSON-карта, созданная vad.py")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help=(
            "Куда сохранить результат (по умолчанию убирается суффикс _vad: "
            "test_vad.srt -> test.srt)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    srt_path = Path(args.srt)
    map_path = Path(args.map)
    output_path = Path(args.output) if args.output else default_output_path(srt_path)

    with map_path.open(encoding="utf-8") as f:
        map_data = json.load(f)

    sampling_rate = float(map_data.get("sampling_rate", 16000))
    mapper = TimeMapper(map_data.get("segments", []), sampling_rate)
    cues = parse_srt(srt_path)

    last_new_end = mapper.points[-1][0]
    last_cue_end = max((cue.end for cue in cues), default=0.0)
    if abs(last_cue_end - last_new_end) > 1.0:
        print(
            f"Предупреждение: SRT заканчивается на {last_cue_end:.3f} с, "
            f"а карта — на {last_new_end:.3f} с. "
            "Возможно, SRT и карта относятся к разным аудиофайлам."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_srt(output_path, cues, mapper)
    print(f"Готово! Реплик пересчитано: {len(cues)}")
    print(f"Результат сохранён в: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
