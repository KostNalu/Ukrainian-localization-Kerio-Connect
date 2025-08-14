#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import os
import sys
import json
import time
import argparse
from typing import Callable, List, Tuple

# ---------------------------
# Допоміжні функції
# ---------------------------

CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')  # будь-яка кирилиця
# JS/JSON рядки: "...." з підтримкою екранувань
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')

# Шаблон для пар "ключ": "значення" — ловимо ДРУГЕ ("значення")
KV_VALUE_RE = re.compile(r'(:\s*)("(?:\\.|[^"\\])*")')

# Плейсхолдери і фрагменти, які треба зберегти 1:1
PLACEHOLDER_PATTERNS = [
    r'%\d+',                      # %1, %2...
    r'\{[^\}]*\}',                # {0}, {name}, {1,number}
    r'<[^>]*?>',                  # HTML теги <br/>, <b>...</b>, <a ...>
    r'&[a-zA-Z#0-9]+;',           # HTML сутності: &nbsp; &amp;
    r'\[\s*[^|\]]+\s*\|\s*[^]]+\]' # [one|many] (залишаємо як є всередині)
]

PLACEHOLDER_RE = re.compile(
    '(' + '|'.join(PLACEHOLDER_PATTERNS) + ')'
)

def mask_fragments(s: str) -> Tuple[str, List[str]]:
    """Маскуємо плейсхолдери/теги, щоб перекладач їх не зіпсував."""
    masks = []
    def _repl(m):
        masks.append(m.group(0))
        return f'__MASK{len(masks)-1}__'
    masked = PLACEHOLDER_RE.sub(_repl, s)
    return masked, masks

def unmask_fragments(s: str, masks: List[str]) -> str:
    for i, val in enumerate(masks):
        s = s.replace(f'__MASK{i}__', val)
    return s

def looks_like_english(s: str) -> bool:
    # якщо немає кирилиці — вважаємо англійським/іншим не-російським
    return CYRILLIC_RE.search(s) is None

def needs_translation(s: str) -> bool:
    # перекладаємо тільки ті строки, де є кирилиця
    return not looks_like_english(s)

# ---------------------------
# Перекладачі
# ---------------------------

class GoogleTranslatorWrapper:
    def __init__(self):
        from deep_translator import GoogleTranslator
        self.tr = GoogleTranslator(source='ru', target='uk')

    def translate(self, text: str) -> str:
        # Google може ламати дуже довгі рядки — перекладемо як є, з ретраями
        for attempt in range(3):
            try:
                return self.tr.translate(text)
            except Exception:
                time.sleep(0.7 * (attempt + 1))
        # якщо не вдалося — повертаємо оригінал (не зіпсуємо файл)
        return text

class ArgosTranslatorWrapper:
    def __init__(self):
        import argostranslate.translate as t
        self.t = t

    def translate(self, text: str) -> str:
        # Argos працює локально; винятки малоймовірні
        try:
            return self.t.translate(text, "ru", "uk")
        except Exception:
            return text

def get_translator(engine: str) -> Callable[[str], str]:
    engine = engine.lower().strip()
    if engine in ("google", "g"):
        return GoogleTranslatorWrapper().translate
    elif engine in ("argos", "a", "offline"):
        return ArgosTranslatorWrapper().translate
    else:
        raise ValueError("Unknown engine. Use 'google' or 'argos'.")

# ---------------------------
# Основна логіка
# ---------------------------

def translate_value_literal(literal: str, translate_func: Callable[[str], str]) -> str:
    """
    Приймає JS/JSON-рядок у лапках (включно з лапками).
    Повертає перекладений рядок (з лапками), якщо там була кирилиця; інакше — як є.
    """
    # знімаємо зовнішні лапки
    assert literal.startswith('"') and literal.endswith('"')
    inner = literal[1:-1]

    # розекрануємо стандартні \"
    # важливо: НЕ чіпаємо всю екранування — лише достатньо для перекладу
    inner_unescaped = inner.replace('\\"', '"')

    if not needs_translation(inner_unescaped):
        return literal  # залишаємо без змін

    # Маскуємо плейсхолдери/HTML
    masked, masks = mask_fragments(inner_unescaped)

    # Переклад
    translated = translate_func(masked)

    # Повертаємо маски
    translated = unmask_fragments(translated, masks)

    # Повертаємо лапки та повторно екранізуємо внутрішні "
    translated_escaped = translated.replace('"', '\\"')
    return f'"{translated_escaped}"'

def process_file(input_path: str, output_path: str, engine: str):
    translate_func = get_translator(engine)

    with open(input_path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

    # Робимо бекап
    backup_path = input_path + ".bak"
    try:
        if not os.path.exists(backup_path):
            with open(backup_path, 'w', encoding='utf-8') as b:
                b.write(text)
    except Exception:
        pass  # якщо бекап не вийшов — не стопаємо процес

    # Стратегія:
    # 1) точково міняємо ЛИШЕ значення після ":", зберігаючи формат та ключі.
    # 2) якщо трапляються рядки поза парами ключ:значення (рідко) — можна додатково пройтись по загальному STRING_RE.

    def _replace_value(m):
        prefix = m.group(1)   # ":   (з проміжками)
        literal = m.group(2)  # "...."
        try:
            return prefix + translate_value_literal(literal, translate_func)
        except Exception:
            # на всяк випадок нічого не ламаємо
            return prefix + literal

    new_text = KV_VALUE_RE.sub(_replace_value, text)

    # Додатковий прохід (опціонально): якщо в файлі є ізольовані значення-рядки без "key":
    # Пропускаємо, щоб не випадково змінити ключі або щось неочікуване.

    with open(output_path, 'w', encoding='utf-8') as out:
        out.write(new_text)

    print(f"Готово! Збережено: {output_path}")
    print(f"Резервна копія: {backup_path}")

def main():
    ap = argparse.ArgumentParser(description="Переклад лише російських значень у JS/JSON локалізаціях на українську.")
    ap.add_argument("input", help="Вхідний файл (napr. ru.js)")
    ap.add_argument("-o", "--output", default=None, help="Вихідний файл (napr. ua.js). Якщо не вказано — додасть .uk")
    ap.add_argument("--engine", default="google", choices=["google", "argos"], help="Двигун перекладу: google (онлайн) або argos (офлайн)")
    args = ap.parse_args()

    input_path = args.input
    if not os.path.isfile(input_path):
        print(f"Файл не знайдено: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or re.sub(r'(\.\w+)?$', '.uk.js', input_path)

    process_file(input_path, output_path, args.engine)

if __name__ == "__main__":
    main()
