#!/usr/bin/env python3
"""Универсальный перезапускаемый пайплайн обработки субтитров.

Все шаги описаны в make_subtitles.json и выполняются строго по порядку.
Скрипт не знает ничего о конкретных программах: он подставляет пути,
проверяет хэши параметров и запускает команды.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TAIL_LIMIT = 20_000
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_-]+))?\}")
STEP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REF_RE = re.compile(r"^@([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")
SUCCESS_STATUSES = {"success", "adopted"}


class PipelineError(Exception):
    """Ошибка пайплайна с понятным сообщением."""


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------


@dataclass
class CommandDef:
    program: str
    args: List[str]
    capture: Optional[str] = None
    default: Any = None
    has_default: bool = False
    validate: Optional[re.Pattern[str]] = None


@dataclass
class Step:
    index: int
    name: str
    description: str
    raw: Dict[str, Any]
    inputs: Dict[str, str]
    outputs: Dict[str, str]
    variables: Dict[str, Any]
    commands: List[CommandDef]


@dataclass
class Config:
    path: Path
    work_dir: Path
    state_file_template: str
    variables: Dict[str, Any]
    steps: List[Step]


@dataclass
class Runtime:
    video: Path
    config: Config
    builtins: Dict[str, str]


@dataclass
class PreparedCommand:
    definition: CommandDef
    hash_argv: List[str]
    value: Optional[str] = None
    result: Optional[dict] = None
    resolved_program: Optional[str] = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        raise PipelineError(f"значение должно быть скаляром: {value!r}")
    return str(value)


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(str(path)))


def substitute(template: str, ctx: Dict[str, Dict[str, Any]], step_name: str) -> str:
    if not isinstance(template, str):
        raise PipelineError(f"шаг '{step_name}': ожидалась строка, получено {type(template).__name__}")

    def repl(match: re.Match[str]) -> str:
        section = match.group(1)
        key = match.group(2)
        if key is None:
            if section in ctx["builtins"]:
                return format_value(ctx["builtins"][section])
            raise PipelineError(f"шаг '{step_name}': неизвестная подстановка {{{section}}}")
        values = ctx.get(section)
        if not isinstance(values, dict) or key not in values:
            raise PipelineError(f"шаг '{step_name}': неизвестная подстановка {{{section}.{key}}}")
        return format_value(values[key])

    return PLACEHOLDER_RE.sub(repl, template)


def _parse_name_map(raw: Any, field_name: str, step_name: str) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PipelineError(f"шаг '{step_name}': поле '{field_name}' должно быть объектом")
    result: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise PipelineError(
                f"шаг '{step_name}': недопустимое имя '{key}' в поле '{field_name}' "
                "(разрешены буквы, цифры, '_' и '-')"
            )
        if not isinstance(value, str) or not value:
            raise PipelineError(f"шаг '{step_name}': значение '{field_name}.{key}' должно быть непустой строкой")
        result[key] = value
    return result


def _parse_variables(raw: Any, owner: str) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PipelineError(f"{owner}: поле 'variables' должно быть объектом")
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise PipelineError(f"{owner}: недопустимое имя переменной '{key}'")
        if isinstance(value, (dict, list)):
            raise PipelineError(f"{owner}: переменная '{key}' должна быть скаляром")
        result[key] = value
    return result


def _parse_commands(raw: Dict[str, Any], step_name: str, variables: Dict[str, Any]) -> List[CommandDef]:
    has_commands = "commands" in raw
    has_program = "program" in raw
    has_args = "args" in raw
    if has_commands and (has_program or has_args):
        raise PipelineError(f"шаг '{step_name}': используйте либо 'program'/'args', либо 'commands'")
    if has_commands:
        command_items = raw["commands"]
    elif has_program:
        command_items = [{"program": raw["program"], "args": raw.get("args", [])}]
    else:
        raise PipelineError(f"шаг '{step_name}': не задана программа ('program' или 'commands')")

    if not isinstance(command_items, list) or not command_items:
        raise PipelineError(f"шаг '{step_name}': 'commands' должен быть непустым массивом")

    commands: List[CommandDef] = []
    captures: set[str] = set()
    seen_main = False
    for index, item in enumerate(command_items):
        if not isinstance(item, dict):
            raise PipelineError(f"шаг '{step_name}': команда #{index + 1} должна быть объектом")
        program = item.get("program")
        args = item.get("args", [])
        if not isinstance(program, str) or not program:
            raise PipelineError(f"шаг '{step_name}': команда #{index + 1}: 'program' должен быть непустой строкой")
        if not isinstance(args, list):
            raise PipelineError(f"шаг '{step_name}': команда #{index + 1}: 'args' должен быть массивом")
        for arg in args:
            if isinstance(arg, (dict, list)):
                raise PipelineError(f"шаг '{step_name}': команда #{index + 1}: аргумент должен быть скаляром")

        capture = item.get("capture")
        if capture is not None:
            if not isinstance(capture, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", capture):
                raise PipelineError(f"шаг '{step_name}': команда #{index + 1}: недопустимое имя capture '{capture}'")
            if capture in captures:
                raise PipelineError(f"шаг '{step_name}': capture '{capture}' указан дважды")
            if capture in variables:
                raise PipelineError(f"шаг '{step_name}': capture '{capture}' конфликтует с variables")
            if seen_main:
                raise PipelineError(
                    f"шаг '{step_name}': команды с 'capture' должны идти до обычных команд"
                )
            captures.add(capture)
        else:
            seen_main = True

        validate = item.get("validate")
        compiled_validate = None
        if validate is not None:
            if not isinstance(validate, str):
                raise PipelineError(f"шаг '{step_name}': команда #{index + 1}: 'validate' должен быть строкой")
            try:
                compiled_validate = re.compile(validate)
            except re.error as exc:
                raise PipelineError(
                    f"шаг '{step_name}': команда #{index + 1}: некорректное регулярное выражение: {exc}"
                ) from exc

        commands.append(
            CommandDef(
                program=program,
                args=[format_value(arg) for arg in args],
                capture=capture,
                default=item.get("default"),
                has_default="default" in item,
                validate=compiled_validate,
            )
        )
    return commands


def load_config(path: Path) -> Config:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PipelineError(f"конфиг не найден: {path}") from exc
    except OSError as exc:
        raise PipelineError(f"не удалось прочитать конфиг {path}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"некорректный JSON в {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError("корень конфига должен быть объектом")

    work_dir_value = raw.get("work_dir", ".")
    if not isinstance(work_dir_value, str) or not work_dir_value:
        raise PipelineError("'work_dir' должен быть непустой строкой")
    work_dir = resolve_path(work_dir_value, path.parent)

    state_file_template = raw.get("state_file", "{stem}.pipeline.json")
    if not isinstance(state_file_template, str) or not state_file_template:
        raise PipelineError("'state_file' должен быть непустой строкой")

    variables = _parse_variables(raw.get("variables"), "конфиг")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PipelineError("'steps' должен быть непустым массивом")

    steps: List[Step] = []
    step_names: set[str] = set()
    known_outputs: Dict[str, set[str]] = {}

    for index, step_raw in enumerate(steps_raw):
        if not isinstance(step_raw, dict):
            raise PipelineError(f"шаг #{index + 1}: должен быть объектом")
        name = step_raw.get("name")
        if not isinstance(name, str) or not name or not STEP_NAME_RE.fullmatch(name):
            raise PipelineError(
                f"шаг #{index + 1}: 'name' должен содержать только буквы, цифры, '_' и '-'"
            )
        if name in step_names:
            raise PipelineError(f"шаг '{name}': имя уже используется")
        step_names.add(name)

        description = step_raw.get("description", "")
        if not isinstance(description, str):
            raise PipelineError(f"шаг '{name}': 'description' должен быть строкой")

        inputs = _parse_name_map(step_raw.get("inputs"), "inputs", name)
        outputs = _parse_name_map(step_raw.get("outputs"), "outputs", name)
        step_variables = _parse_variables(step_raw.get("variables"), f"шаг '{name}'")
        commands = _parse_commands(step_raw, name, step_variables)

        for input_name, input_value in inputs.items():
            if input_value.startswith("@"):
                match = REF_RE.fullmatch(input_value)
                if not match:
                    raise PipelineError(
                        f"шаг '{name}': вход '{input_name}': ссылка должна иметь вид @step.output"
                    )
                ref_step, ref_output = match.groups()
                if ref_step not in known_outputs:
                    raise PipelineError(
                        f"шаг '{name}': вход '{input_name}' ссылается на шаг '{ref_step}', "
                        "который идёт позже или не существует"
                    )
                if ref_output not in known_outputs[ref_step]:
                    raise PipelineError(
                        f"шаг '{name}': у шага '{ref_step}' нет выхода '{ref_output}'"
                    )

        for output_name, output_value in outputs.items():
            if output_value.startswith("@"):
                raise PipelineError(
                    f"шаг '{name}': выход '{output_name}' не может быть ссылкой @step.output"
                )

        known_outputs[name] = set(outputs)
        steps.append(
            Step(
                index=index,
                name=name,
                description=description,
                raw=step_raw,
                inputs=inputs,
                outputs=outputs,
                variables=step_variables,
                commands=commands,
            )
        )

    return Config(
        path=path,
        work_dir=work_dir,
        state_file_template=state_file_template,
        variables=variables,
        steps=steps,
    )


def make_runtime(config: Config, video: Path) -> Runtime:
    builtins = {
        "video": str(video),
        "video_dir": str(video.parent),
        "video_name": video.name,
        "stem": video.stem,
        "work_dir": str(config.work_dir),
        "config_dir": str(config.path.parent),
    }
    return Runtime(video=video, config=config, builtins=builtins)


def resolve_state_path(runtime: Runtime) -> Path:
    ctx = {
        "builtins": runtime.builtins,
        "inputs": {},
        "outputs": {},
        "vars": {},
    }
    value = substitute(runtime.config.state_file_template, ctx, "<state_file>")
    return resolve_path(value, runtime.config.work_dir)


# ---------------------------------------------------------------------------
# Разрешение путей шага
# ---------------------------------------------------------------------------


def resolve_step_paths(
    step: Step,
    runtime: Runtime,
    previous_outputs: Dict[str, Dict[str, str]],
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[str, Dict[str, Any]]]:
    vars_ctx: Dict[str, str] = {}
    ctx: Dict[str, Dict[str, Any]] = {
        "builtins": runtime.builtins,
        "inputs": {},
        "outputs": {},
        "vars": vars_ctx,
    }

    for key, value in runtime.config.variables.items():
        vars_ctx[key] = substitute(format_value(value), ctx, "конфиг")

    for key, value in step.variables.items():
        vars_ctx[key] = substitute(format_value(value), ctx, f"шаг '{step.name}'")

    inputs: Dict[str, str] = {}
    for name, raw_value in step.inputs.items():
        if raw_value.startswith("@"):
            match = REF_RE.fullmatch(raw_value)
            if not match:
                raise PipelineError(
                    f"шаг '{step.name}': вход '{name}': ссылка должна иметь вид @step.output"
                )
            ref_step, ref_output = match.groups()
            try:
                inputs[name] = previous_outputs[ref_step][ref_output]
            except KeyError as exc:
                raise PipelineError(
                    f"шаг '{step.name}': вход '{name}' ссылается на неизвестный выход "
                    f"'{ref_step}.{ref_output}'"
                ) from exc
        else:
            value = substitute(raw_value, ctx, step.name)
            inputs[name] = str(resolve_path(value, runtime.config.work_dir))

    ctx["inputs"] = inputs

    outputs: Dict[str, str] = {}
    for name, raw_value in step.outputs.items():
        if raw_value.startswith("@"):
            raise PipelineError(
                f"шаг '{step.name}': выход '{name}' не может быть ссылкой @step.output"
            )
        value = substitute(raw_value, ctx, step.name)
        outputs[name] = str(resolve_path(value, runtime.config.work_dir))

    ctx["outputs"] = outputs
    return inputs, outputs, vars_ctx, ctx


# ---------------------------------------------------------------------------
# Программы и команды
# ---------------------------------------------------------------------------


def resolve_program(program: str) -> str:
    if os.path.sep in program or (os.path.altsep and os.path.altsep in program) or program.startswith("~"):
        path = Path(program).expanduser()
        if not path.is_file():
            raise PipelineError(f"программа не найдена: {program}")
        if not os.access(str(path), os.X_OK):
            raise PipelineError(f"нет права на выполнение: {program}")
        return str(path)
    found = shutil.which(program)
    if not found:
        raise PipelineError(f"программа не найдена в PATH: {program}")
    return found


def write_bytes(data: bytes) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def stream_process(proc: subprocess.Popen[bytes]) -> Tuple[int, str]:
    """Пишет stdout/stderr процесса в консоль по мере поступления и возвращает tail."""
    assert proc.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    tail_parts: List[str] = []
    tail_len = 0
    last_byte = b""

    def append_tail(text: str) -> None:
        nonlocal tail_len
        if not text:
            return
        tail_parts.append(text)
        tail_len += len(text)
        while tail_len > TAIL_LIMIT and len(tail_parts) > 1:
            tail_len -= len(tail_parts.pop(0))

    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 65_536)
            if not chunk:
                break
            last_byte = chunk[-1:]
            write_bytes(chunk)
            append_tail(decoder.decode(chunk))
        append_tail(decoder.decode(b"", final=True))
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        proc.stdout.close()

    if last_byte not in (b"\n",):
        write_bytes(b"\n")
    return returncode, "".join(tail_parts)[-TAIL_LIMIT:]


def _command_record(program: str, argv: List[str], cwd: Path, status: str, **extra: Any) -> dict:
    record = {
        "program": program,
        "argv": argv,
        "command_line": shlex.join(argv),
        "status": status,
        "cwd": str(cwd),
    }
    record.update(extra)
    return record


def run_capture(step: Step, prepared: PreparedCommand, runtime: Runtime) -> None:
    cdef = prepared.definition
    assert cdef.capture is not None
    try:
        program = resolve_program(prepared.hash_argv[0])
    except PipelineError as exc:
        raise PipelineError(f"шаг '{step.name}': {exc}") from exc

    argv = [program, *prepared.hash_argv[1:]]
    print(f"  $ {shlex.join(argv)}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(runtime.config.work_dir),
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise PipelineError(f"шаг '{step.name}': не удалось запустить {program}: {exc}") from exc

    duration = time.monotonic() - started
    raw_value = (completed.stdout or "").strip()
    valid = completed.returncode == 0 and bool(raw_value)
    if valid and cdef.validate is not None:
        valid = cdef.validate.fullmatch(raw_value) is not None

    value = raw_value
    status = "success"
    if not valid:
        if cdef.has_default:
            value = format_value(cdef.default)
            status = "fallback"
            print(
                f"  ! capture '{cdef.capture}' не удался (код {completed.returncode}), "
                f"использую значение по умолчанию: {value!r}"
            )
        else:
            raise PipelineError(
                f"шаг '{step.name}': capture '{cdef.capture}' завершился с ошибкой "
                f"(код {completed.returncode}, вывод: {raw_value!r})"
            )
    else:
        print(f"  -> {cdef.capture} = {value}")

    prepared.value = value
    prepared.result = _command_record(
        program,
        argv,
        runtime.config.work_dir,
        status,
        returncode=completed.returncode,
        duration_sec=round(duration, 3),
        captured=cdef.capture,
        value=value,
    )


def build_commands(
    step: Step,
    ctx: Dict[str, Dict[str, Any]],
    runtime: Runtime,
) -> List[PreparedCommand]:
    prepared_commands: List[PreparedCommand] = []
    for cdef in step.commands:
        program = substitute(cdef.program, ctx, step.name)
        args = [substitute(arg, ctx, step.name) for arg in cdef.args]
        prepared = PreparedCommand(definition=cdef, hash_argv=[program, *args])
        if cdef.capture is not None:
            run_capture(step, prepared, runtime)
            assert prepared.value is not None
            ctx["vars"][cdef.capture] = prepared.value
        prepared_commands.append(prepared)
    return prepared_commands


# ---------------------------------------------------------------------------
# Хэши, статистика и state
# ---------------------------------------------------------------------------


def file_stat(path: str) -> dict:
    st = os.stat(path)
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_info(path: str) -> dict:
    st = os.stat(path)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def compute_hash(
    step: Step,
    runtime: Runtime,
    inputs: Dict[str, str],
    outputs: Dict[str, str],
    prepared: List[PreparedCommand],
    captured: Dict[str, str],
) -> str:
    payload = {
        "step": step.name,
        "video": file_stat(str(runtime.video)),
        "inputs": {name: file_stat(path) for name, path in sorted(inputs.items())},
        "outputs": dict(sorted(outputs.items())),
        "commands": [command.hash_argv for command in prepared],
        "captured": captured,
        "variables": step.variables,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def command_records(
    prepared: List[PreparedCommand],
    cwd: Path,
    pending_status: str = "not_run",
) -> List[dict]:
    records: List[dict] = []
    for command in prepared:
        if command.result is not None:
            records.append(command.result)
            continue
        program = command.resolved_program or command.hash_argv[0]
        argv = [program, *command.hash_argv[1:]]
        records.append(_command_record(program, argv, cwd, pending_status))
    return records


def base_record(
    step: Step,
    runtime: Runtime,
    status: str,
    hash_value: Optional[str],
    inputs: Dict[str, str],
    outputs: Dict[str, str],
    captured: Dict[str, str],
    commands: List[dict],
    duration: Optional[float],
    hash_outputs: bool,
    **extra: Any,
) -> dict:
    input_stats: Dict[str, dict] = {}
    for name, path in inputs.items():
        try:
            input_stats[name] = file_stat(path)
        except OSError:
            input_stats[name] = {"path": path, "missing": True}

    output_stats: Dict[str, dict] = {}
    for name, path in outputs.items():
        if hash_outputs and os.path.isfile(path):
            output_stats[name] = output_info(path)
            continue
        item: Dict[str, Any] = {"path": path, "exists": os.path.isfile(path)}
        try:
            st = os.stat(path)
        except OSError:
            pass
        else:
            item["size"] = st.st_size
            item["mtime_ns"] = st.st_mtime_ns
        output_stats[name] = item

    record = {
        "time": now_iso(),
        "step": step.name,
        "description": step.description,
        "status": status,
        "hash": hash_value,
        "video": str(runtime.video),
        "params": step.raw,
        "variables": step.variables,
        "captured": captured,
        "inputs": input_stats,
        "outputs": output_stats,
        "commands": commands,
        "duration_sec": round(duration, 3) if duration is not None else None,
    }
    record.update(extra)
    return record


class State:
    def __init__(self, path: Path, video: Path) -> None:
        self.path = path
        self.video = str(video)
        self.data: Dict[str, Any] = {"version": 1, "video": self.video, "runs": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
                raise ValueError("некорректная структура state-файла")
        except Exception as exc:  # noqa: BLE001 - повреждённый state не должен блокировать работу
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                self.path.replace(backup)
            except OSError:
                pass
            print(f"  ! не удалось прочитать state {self.path}: {exc}", file=sys.stderr)
            print(f"  ! старый state сохранён как {backup}", file=sys.stderr)
            return
        self.data = data
        self.data.setdefault("version", 1)
        self.data["video"] = self.video

    def latest(self, step_name: str) -> Optional[dict]:
        for record in reversed(self.data["runs"]):
            if isinstance(record, dict) and record.get("step") == step_name:
                return record
        return None

    def append(self, record: dict) -> None:
        self.data["runs"].append(record)
        self.save()

    def save(self) -> None:
        temp_path = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise PipelineError(f"не удалось записать state {self.path}: {exc}") from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Выполнение шага
# ---------------------------------------------------------------------------


def run_main(prepared: PreparedCommand, cwd: Path) -> dict:
    assert prepared.resolved_program is not None
    argv = [prepared.resolved_program, *prepared.hash_argv[1:]]
    print(f"  $ {shlex.join(argv)}", flush=True)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except OSError as exc:
        duration = time.monotonic() - started
        return _command_record(
            prepared.resolved_program,
            argv,
            cwd,
            "failed",
            returncode=None,
            duration_sec=round(duration, 3),
            error=str(exc),
        )

    returncode, tail = stream_process(proc)
    duration = time.monotonic() - started
    status = "success" if returncode == 0 else "failed"
    extra: Dict[str, Any] = {
        "returncode": returncode,
        "duration_sec": round(duration, 3),
    }
    if status == "failed":
        extra["output_tail"] = tail
    return _command_record(prepared.resolved_program, argv, cwd, status, **extra)


def process_step(
    step: Step,
    runtime: Runtime,
    state: State,
    force: bool,
    dry_run: bool,
    previous_outputs: Dict[str, Dict[str, str]],
) -> Tuple[str, Dict[str, str]]:
    total = len(runtime.config.steps)
    title = f"[{step.index + 1}/{total}] {step.name}"
    if step.description:
        title += f" — {step.description}"
    print(f"\n=== {title} ===", flush=True)

    inputs, outputs, _vars_ctx, ctx = resolve_step_paths(step, runtime, previous_outputs)
    for name, path in inputs.items():
        print(f"  вход  {name}: {path}")
    for name, path in outputs.items():
        print(f"  выход {name}: {path}")

    prepared = build_commands(step, ctx, runtime)
    captured = {
        command.definition.capture: command.value
        for command in prepared
        if command.definition.capture is not None and command.value is not None
    }

    missing_inputs = [f"{name}={path}" for name, path in inputs.items() if not os.path.isfile(path)]
    if missing_inputs:
        if dry_run:
            print("  ! входные файлы ещё не созданы: " + ", ".join(missing_inputs))
            hash_value: Optional[str] = None
        else:
            raise PipelineError(
                f"шаг '{step.name}': не найдены входные файлы: " + ", ".join(missing_inputs)
            )
    else:
        hash_value = compute_hash(step, runtime, inputs, outputs, prepared, captured)

    outputs_ok = bool(outputs) and all(os.path.isfile(path) for path in outputs.values())
    latest = state.latest(step.name)
    skip = False
    adopt = False
    if not force and hash_value is not None and outputs_ok:
        if (
            latest is not None
            and latest.get("hash") == hash_value
            and latest.get("status") in SUCCESS_STATUSES
        ):
            skip = True
        elif latest is None:
            adopt = True

    short_hash = hash_value[:19] + "…" if hash_value else "хэш не рассчитан"

    def make_record(
        status: str,
        *,
        commands: List[dict],
        duration: Optional[float] = None,
        hash_outputs: bool = False,
        error: Optional[str] = None,
    ) -> dict:
        extra = {"error": error} if error else {}
        return base_record(
            step,
            runtime,
            status,
            hash_value,
            inputs,
            outputs,
            captured,
            commands,
            duration,
            hash_outputs,
            **extra,
        )

    if skip:
        print(f"  SKIP: выходные файлы на месте, параметры не менялись ({short_hash})")
        return "skip", outputs

    if adopt:
        if dry_run:
            print(f"  SKIP (adopt): выходные файлы есть, но записи в state нет ({short_hash})")
        else:
            print(
                f"  ADOPT: выходные файлы есть, записи в state нет — "
                f"принимаю как готовые ({short_hash})"
            )
            state.append(
                make_record(
                    "adopted",
                    commands=command_records(prepared, runtime.config.work_dir),
                    hash_outputs=True,
                )
            )
        return "adopt", outputs

    reason = " (принудительно)" if force else ""
    print(f"  RUN{reason} ({short_hash})")

    if dry_run:
        for command in prepared:
            if command.definition.capture is not None:
                continue
            try:
                program = resolve_program(command.hash_argv[0])
                missing_program = False
            except PipelineError:
                program = command.hash_argv[0]
                missing_program = True
            argv = [program, *command.hash_argv[1:]]
            print(f"  $ {shlex.join(argv)}")
            if missing_program:
                print(f"    ! программа не найдена: {program}")
        return "run", outputs

    try:
        for path in outputs.values():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        state.append(
            make_record(
                "failed",
                commands=command_records(prepared, runtime.config.work_dir),
                error=str(exc),
            )
        )
        raise PipelineError(f"шаг '{step.name}': не удалось создать папку выхода: {exc}") from exc

    try:
        for command in prepared:
            if command.definition.capture is None:
                command.resolved_program = resolve_program(command.hash_argv[0])
    except PipelineError as exc:
        state.append(
            make_record(
                "failed",
                commands=command_records(prepared, runtime.config.work_dir),
                error=str(exc),
            )
        )
        raise PipelineError(f"шаг '{step.name}': {exc}") from exc

    state.append(
        make_record(
            "running",
            commands=command_records(prepared, runtime.config.work_dir, pending_status="pending"),
        )
    )

    started = time.monotonic()
    for command in prepared:
        if command.definition.capture is not None:
            continue
        result = run_main(command, runtime.config.work_dir)
        command.result = result
        if result["status"] != "success":
            duration = time.monotonic() - started
            state.append(
                make_record(
                    "failed",
                    commands=command_records(prepared, runtime.config.work_dir),
                    duration=duration,
                    error=f"команда завершилась с кодом {result.get('returncode')}",
                )
            )
            raise PipelineError(
                f"шаг '{step.name}': команда завершилась с кодом {result.get('returncode')}"
            )

    duration = time.monotonic() - started
    missing_outputs = [f"{name}={path}" for name, path in outputs.items() if not os.path.isfile(path)]
    if missing_outputs:
        state.append(
            make_record(
                "failed",
                commands=command_records(prepared, runtime.config.work_dir),
                duration=duration,
                error="не созданы выходные файлы: " + ", ".join(missing_outputs),
            )
        )
        raise PipelineError(
            f"шаг '{step.name}': не созданы выходные файлы: " + ", ".join(missing_outputs)
        )

    state.append(
        make_record(
            "success",
            commands=command_records(prepared, runtime.config.work_dir),
            duration=duration,
            hash_outputs=True,
        )
    )
    print(f"  OK ({duration:.1f} c)")
    return "run", outputs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Перезапускаемый пайплайн обработки субтитров, шаги задаются в JSON."
    )
    parser.add_argument("input", help="путь к исходному видео")
    parser.add_argument(
        "--config",
        default=str(script_dir / "make_subtitles.json"),
        help="путь к JSON-конфигу (по умолчанию рядом со скриптом)",
    )
    parser.add_argument("--force", action="store_true", help="выполнить все шаги заново")
    parser.add_argument(
        "--from",
        dest="from_step",
        metavar="STEP",
        help="выполнить указанный шаг и все последующие заново",
    )
    parser.add_argument(
        "--force-step",
        action="append",
        default=[],
        metavar="STEP",
        help="выполнить только указанный шаг заново (можно повторять)",
    )
    parser.add_argument(
        "--python",
        metavar="PATH",
        help=(
            "Python-интерпретатор для шагов, где указано {vars.python} "
            "(например, cut/.venv/bin/python)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать решения и команды, не выполняя изменяющие команды",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load_config(config_path)

        if args.python:
            config.variables["python"] = str(resolve_path(args.python, Path.cwd()))

        video = Path(args.input).expanduser().resolve()
        if not video.is_file():
            raise PipelineError(f"видео не найдено: {video}")

        step_names = [step.name for step in config.steps]
        for name in args.force_step:
            if name not in step_names:
                raise PipelineError(f"--force-step: неизвестный шаг '{name}'")
        from_index: Optional[int] = None
        if args.from_step:
            if args.from_step not in step_names:
                raise PipelineError(f"--from: неизвестный шаг '{args.from_step}'")
            from_index = step_names.index(args.from_step)

        runtime = make_runtime(config, video)
        try:
            config.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PipelineError(f"не удалось создать рабочую папку {config.work_dir}: {exc}") from exc
        state_path = resolve_state_path(runtime)
        state = State(state_path, video)

        print(f"Конфиг: {config.path}")
        print(f"Видео: {video}")
        print(f"Рабочая папка: {config.work_dir}")
        print(f"Состояние: {state_path}")
        if args.dry_run:
            print("Режим: dry-run (изменяющие команды не выполняются)")

        counters = {"run": 0, "skip": 0, "adopt": 0}
        previous_outputs: Dict[str, Dict[str, str]] = {}

        for step in config.steps:
            force = (
                args.force
                or step.name in args.force_step
                or (from_index is not None and step.index >= from_index)
            )
            action, outputs = process_step(
                step,
                runtime,
                state,
                force,
                args.dry_run,
                previous_outputs,
            )
            previous_outputs[step.name] = outputs
            counters[action] += 1

        print()
        if args.dry_run:
            print(
                f"Dry-run: запустилось бы {counters['run']}, "
                f"пропущено {counters['skip']}, принято {counters['adopt']}."
            )
        else:
            print(
                f"Готово: выполнено {counters['run']}, "
                f"пропущено {counters['skip']}, принято {counters['adopt']}."
            )
        return 0
    except PipelineError as exc:
        print(f"\nОШИБКА: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
