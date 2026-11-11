import re
from pathlib import Path
import sys
from datetime import timedelta
import ass
import ass.data
import os
import argparse
os.environ["LIBASS_PATH"] = "/opt/homebrew/opt/libass/lib/libass.dylib"
os.environ["DYLD_LIBRARY_PATH"] = "/opt/homebrew/opt/libass/lib"


def srt_time_to_ms(time_str: str) -> int:
    """Переводит время SRT в миллисекунды"""
    h, m, s = time_str.replace(',', '.').split(':')
    return int((int(h)*3600 + int(m)*60 + float(s)) * 1000)


def read_srt_file(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    result = {}
    blocks = re.split(r'\n\s*\n(?=\d+\n)', content.strip() + '\n')
    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        result[lines[1]] = " ".join(lines[2:])
    return result


def main(input_file, aspect_ratio):

    input_path = Path(input_file)
    input_ru_path = Path(input_file[:-4] + "_ru" + input_file[-4:])
    if not input_path.exists():
        print(f"Файл {input_file} не найден!")
        return
    if not input_ru_path.exists():
        print(f"Файл {input_ru_path} не найден!")
        return

    width, height = map(int, aspect_ratio.split(':'))
    # нужно чтобы width был примерно 2000
    scale_factor = 2000 / width
    width = int(width * scale_factor)
    height = int(height * scale_factor)

    output_path = input_path.with_name(input_path.stem + ".ass")

    origin_subtitles = read_srt_file(input_file)
    tr_subtitles = read_srt_file(input_ru_path)

    doc = ass.Document()
    doc.info['Title'] = 'Bilingual Greek-Russian'
    doc.info['ScriptType'] = 'v4.00+'
    doc.info['WrapStyle'] = '0'
    doc.info['PlayResX'] = str(width)
    doc.info['PlayResY'] = str(height)
    doc.info['ScaledBorderAndShadow'] = 'yes'

    color_primary = ass.data.Color(r=255, g=255, b=255, a=0)
    color_secondary = ass.data.Color(r=255, g=0, b=0, a=0)
    color_outline = ass.data.Color(r=0, g=0, b=0, a=0)
    color_back = ass.data.Color(r=0, g=0, b=0, a=128)

    doc.styles.append(ass.Style(
        name='Greek', fontname='Arial', fontsize=72,
        primary_color=color_primary, secondary_color=color_secondary, 
        outline_color=color_outline, back_color=color_back,
        bold=False, italic=False, underline=False, strike_out=False,
        scale_x=100, scale_y=100, spacing=0, angle=0, border_style=1, 
        outline=2, shadow=2, alignment=2, margin_l=10, margin_r=10, margin_v=30, encoding=1
    ))
    doc.styles.append(ass.Style(
        name='Russian', fontname='Arial', fontsize=42,
        primary_color=color_primary, secondary_color=color_secondary, 
        outline_color=color_outline, back_color=color_back,
        bold=False, italic=False, underline=False, strike_out=False,
        scale_x=100, scale_y=100, spacing=0, angle=0, border_style=1, 
        outline=2, shadow=1, alignment=2, margin_l=10, margin_r=10, margin_v=8, encoding=1
    ))

    X = width // 2
    Y_BOTTOM = height
    GAP = 20

    try:
        from ass_renderer.libass import AssLibrary, _libass
        import ctypes
        import io
        ctx = AssLibrary()
        renderer = ctx.make_renderer()
        renderer.set_fonts()
        renderer.storage_size = (width, height)
        renderer.frame_size = (width, height)
        
        def measure_text(styles, text):
            temp_doc = ass.Document()
            temp_doc.info['PlayResX'] = str(width)
            temp_doc.info['PlayResY'] = str(height)
            for s in styles:
                temp_doc.styles.append(s)
            
            ev = ass.Dialogue(
                start=timedelta(seconds=0),
                end=timedelta(seconds=2),
                style=styles[0].name,
                text=text
            )
            temp_doc.events.append(ev)
            
            track = ctx.make_track()
            f = io.StringIO()
            temp_doc.dump_file(f)
            ass_data = f.getvalue().encode('utf-8')
            _libass.ass_process_data(ctypes.byref(track), ass_data, len(ass_data))
            
            imgs = renderer.render_frame(track, 100)

            min_y, max_y = 1e9, -1e9

            for img in imgs:
                y1 = img.dst_y
                y2 = y1 + img.h
                min_y = min(min_y, y1)
                max_y = max(max_y, y2)

            if min_y > max_y:
                return 150

            return max_y - min_y
            
    except ImportError as e:
        print(e)
        print("ass_renderer not found, using simple metrics 150 pixels per line")
        def measure_text(styles, text):
            return 150

    subs = []
    last_end_ms = 0
    for timing_srt, text_origin in origin_subtitles.items():
        text_translate = tr_subtitles.get(timing_srt, "")
        if not text_translate:
            print(f"No translation for text: {text_origin} in timing: {timing_srt}")

        start_str, end_str = timing_srt.split(' --> ')
        start_ms = srt_time_to_ms(start_str)
        if start_ms - 1000 > last_end_ms:
            start_ms -= 1000
        else:
            start_ms = last_end_ms
        last_end_ms = srt_time_to_ms(end_str)
        end_ms = last_end_ms + 2000
        
        raw_text = f"{text_origin}{{\\fs42\\shad1}}\\N{text_translate}"
        subs.append({
            'start': start_ms,
            'end': end_ms,
            'text': raw_text
        })
        
    subs.sort(key=lambda x: x['start'])

    prev_sub = None

    EFFECT_DURATION = 100

    for i, sub in enumerate(subs):
        start_ms = sub['start']
        end_ms = sub['end']
        raw_text = sub['text']
        
        # Determine how long the subtitle stays at the bottom
        # It stays at the bottom until it ends OR until just before the next subtitle appears
        if i + 1 < len(subs):
            next_start = subs[i+1]['start']
            bottom_end_ms = min(end_ms, next_start - EFFECT_DURATION)
        else:
            bottom_end_ms = end_ms
            
        text_for_measure = f"{{\\an2\\pos({X},{Y_BOTTOM})}}{raw_text}"
        h_new = measure_text(doc.styles, text_for_measure)
        sub['h'] = h_new
        
        # 1. Add the "bottom" part of the subtitle
        if start_ms < bottom_end_ms:
            doc.events.append(ass.Dialogue(
                layer=0, start=timedelta(milliseconds=start_ms), end=timedelta(milliseconds=bottom_end_ms), 
                style='Greek', name='', margin_l=0, margin_r=0, margin_v=0, effect='', 
                text=text_for_measure
            ))
        
        # 2. If the previous subtitle is still active, move it up
        # We start the move EFFECT_DURATION ms before the new sub appears
        if prev_sub and prev_sub['end'] > start_ms - EFFECT_DURATION:
            prev_raw_text = prev_sub['text']
            # It starts moving up slightly before the current subtitle appears
            old_start_ms = start_ms - EFFECT_DURATION
            
            # The previous sub in TOP should end when the NEXT sub appears 
            # (because that's when the current sub will move to TOP)
            if i + 1 < len(subs):
                next_sub_start = subs[i+1]['start']
                old_end_ms = min(prev_sub['end'], next_sub_start - EFFECT_DURATION)
            else:
                old_end_ms = prev_sub['end']
            
            # Use the height of the NEW subtitle to determine how much to move up
            y_top = Y_BOTTOM - int(h_new) - GAP
            
            # \move(x1, y1, x2, y2, t1, t2) -> quick move in 100ms
            old_text = (
                f"{{\\an2"
                f"\\move({X},{Y_BOTTOM},{X},{y_top},0,{EFFECT_DURATION})"
                f"\\fscx85\\fscy85"
                f"\\alpha&H40&"
                f"}}" + prev_raw_text
            )
            
            if old_end_ms > old_start_ms:
                doc.events.append(ass.Dialogue(
                    layer=0, start=timedelta(milliseconds=old_start_ms), end=timedelta(milliseconds=old_end_ms),
                    style='Greek', name='', margin_l=0, margin_r=0, margin_v=0, effect='',
                    text=old_text
                ))
            
        prev_sub = sub

    with open(output_path, "w", encoding="utf-8-sig") as f:
        doc.dump_file(f)

    print(f"\nГотово")
    print(f"Файл создан: {output_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert SRT to ASS with custom screen size")
    parser.add_argument("input_file", help="Input SRT file")
    parser.add_argument("aspect_ratio", help="Aspect ratio", default="16:9")
    args = parser.parse_args()
    
    main(args.input_file, args.aspect_ratio)