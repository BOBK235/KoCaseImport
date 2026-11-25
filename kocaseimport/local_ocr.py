"""Bounded one-page PDF rendering and PyOCR via the libtesseract C API.

Native libraries run in a disposable child with a memory/CPU/wall-clock limit.
This isolates native crashes; it is not an OS security sandbox.
"""
import ctypes
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import resource
import signal
import time
import unicodedata

from .errors import PublicJobError
from .utils import clean_extracted_text, text_is_usable


def _tessdata(cfg):
    if not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', cfg.ocr_local_lang):
        raise PublicJobError(code='ocr_local_models')
    candidates = [Path(cfg.ocr_tessdata_dir)] if cfg.ocr_tessdata_dir else [
        Path(root)/f'usr/share/tesseract-ocr/{version}/tessdata'
        for root in ('/layers/heroku_deb-packages/packages', '/layers/fagiani_apt/apt', '/')
        for version in ('5', '4.00')
    ] + [Path('/usr/share/tessdata')]
    for path in candidates:
        if path.is_absolute() and all((path/(lang+'.traineddata')).is_file() for lang in cfg.ocr_local_lang.split('+')):
            return path
    raise PublicJobError(code='ocr_local_models')


def _render_and_ocr(pdf_path, number, cfg):
    os.environ['OMP_THREAD_LIMIT'] = '1'
    os.environ['OMP_NUM_THREADS'] = '1'
    try:
        import pypdfium2 as pdfium
        from pyocr import libtesseract, builders
    except (ImportError, OSError):
        raise PublicJobError(code='ocr_local_engine') from None
    if not libtesseract.is_available():
        raise PublicJobError(code='ocr_local_engine')
    # Buildpack packages live under /layers, outside Tesseract's compiled path.
    libtesseract.tesseract_raw.TESSDATA_PREFIX = str(_tessdata(cfg))
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except pdfium.PdfiumError:
        raise PublicJobError(code='ocr_local_render') from None
    with document:
        if not 1 <= number <= len(document):
            raise PublicJobError(code='ocr_local_render')
        page = document[number-1]
        bitmap = None
        try:
            width, height = page.get_size()
            if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                raise PublicJobError(code='ocr_local_render')
            # Reserve rounding room because PDFium rounds pixel dimensions up.
            pixel_budget = cfg.ocr_render_pixels - 2*cfg.ocr_render_side - 1
            scale = min(cfg.ocr_render_dpi/72, cfg.ocr_render_side/max(width, height),
                        math.sqrt(pixel_budget/(width*height)))
            bitmap = page.render(scale=scale, grayscale=True)
            image = bitmap.to_pil()
            try:
                if image.width*image.height > cfg.ocr_render_pixels:
                    raise PublicJobError(code='ocr_local_render')
                image.info['dpi'] = (max(70, round(scale*72)),)*2
                if image.getextrema()[0] == 255:
                    return '', 'pyocr-blank'
                text = clean_extracted_text(libtesseract.image_to_string(
                    image, lang=cfg.ocr_local_lang,
                    builder=builders.TextBuilder(tesseract_layout=max(3, min(cfg.ocr_local_psm, 13)))))
                if not text:
                    raise PublicJobError(code='ocr_empty')
                if len(text.encode('utf-8')) > cfg.ocr_page_text_bytes:
                    raise PublicJobError(code='ocr_result_limit')
                return text, 'pyocr-tesseract'
            finally:
                image.close()
        except pdfium.PdfiumError:
            raise PublicJobError(code='ocr_local_render') from None
        finally:
            if bitmap is not None:
                bitmap.close()
            page.close()


def _pdf_text(pdf_path, number, cfg):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(pdf_path)) as document:
        page = document[number - 1]
        try:
            layer = page.get_textpage()
            try:
                if layer.count_chars() > cfg.ocr_page_text_bytes:
                    raise PublicJobError(code='ocr_result_limit')
                # Exclude running headers and folios when looking for body text.
                left, bottom, right, top = page.get_bbox()
                margin = (top - bottom) * 0.12
                middle = layer.get_text_bounded(left, bottom + margin, right, top - margin,
                                                errors='replace')
                if len(re.findall(r'[가-힣]', middle)) < 20 or not text_is_usable(middle, 36):
                    return '', 'pdf-text'
                text = layer.get_text_bounded(errors='replace')
            finally:
                layer.close()
        finally:
            page.close()
    # Missing character maps can yield printable CID placeholders or private-use glyphs.
    if (re.search(r'\(cid:\d+\)|[\ufffd\x00-\x08\x0b\x0e-\x1f]', text, re.I)
            or any(unicodedata.category(ch) == 'Co' for ch in text)
            or len(set(re.findall(r'[가-힣]', middle))) < 8):
        return '', 'pdf-text'
    return clean_extracted_text(text), 'pdf-text'


def _child(send, pdf_path, number, cfg, parent, operation, seconds):
    try:
        # Linux: a hard-killed job must not leave a native OCR child running.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        resource.setrlimit(resource.RLIMIT_AS, (cfg.job_memory_bytes, cfg.job_memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # Native error messages can include filesystem paths; use fixed codes.
        with open(os.devnull, 'wb') as sink:
            os.dup2(sink.fileno(), 2)
        text, source = operation(pdf_path, number, cfg)
        if len(text.encode('utf-8')) > cfg.ocr_page_text_bytes:
            raise PublicJobError(code='ocr_result_limit')
        result = {'text': text, 'source': source}
    except PublicJobError as error:
        result = {'error': error.code}
    except MemoryError:
        result = {'error': 'memory_limit'}
    except Exception:
        result = {'error': 'ocr_local_failed'}
    try:
        send.send_bytes(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    finally:
        send.close()


def recognize_page(pdf_path, number, cfg, *, guard=lambda: None):
    return _run_page(pdf_path, number, cfg, guard, _render_and_ocr,
                     {'pyocr-tesseract', 'pyocr-blank'}, max(1, min(cfg.ocr_timeout, 120)))


def extract_page_text(pdf_path, number, cfg, *, guard=lambda: None):
    try:
        text, _ = _run_page(pdf_path, number, cfg, guard, _pdf_text, {'pdf-text'},
                            max(1, min(cfg.ocr_timeout, 8)))
        return text
    except PublicJobError as error:
        if error.code not in {'ocr_local_failed', 'ocr_local_timeout'}:
            raise
        return ''


def _run_page(pdf_path, number, cfg, guard, operation, sources, seconds):
    guard()
    ctx = multiprocessing.get_context('fork')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(send, pdf_path, number, cfg, os.getpid(), operation, seconds))
    started = False
    try:
        process.start(); started = True; send.close()
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise PublicJobError(code='ocr_local_timeout')
            if receive.poll(min(1, remaining)):
                try:
                    result = json.loads(receive.recv_bytes(cfg.ocr_response_bytes))
                except (EOFError, OSError, ValueError):
                    raise PublicJobError(code='ocr_local_failed') from None
                guard()
                if not isinstance(result, dict):
                    raise PublicJobError(code='ocr_local_failed')
                if result.get('error'):
                    raise PublicJobError(code=result['error'])
                text, source = result.get('text'), result.get('source')
                if not isinstance(text, str) or source not in sources:
                    raise PublicJobError(code='ocr_local_failed')
                if len(text.encode('utf-8')) > cfg.ocr_page_text_bytes:
                    raise PublicJobError(code='ocr_result_limit')
                return text, source
            guard()
            if not process.is_alive():
                raise PublicJobError(code='ocr_local_failed')
    finally:
        receive.close(); send.close()
        if started:
            process.join(0.1)
            if process.is_alive():
                process.terminate(); process.join(1)
                if process.is_alive():
                    process.kill(); process.join()
            process.close()
