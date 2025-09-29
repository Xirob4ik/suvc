import logging
import requests
from bs4 import BeautifulSoup
import html
import os
import json # Для сохранения и загрузки списка подписчиков
import re # Для поиска дней недели и форматирования
from datetime import datetime, timedelta # Для работы с датами
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Настройки ---
BOT_TOKEN = "7611833704:AAHZ8fvHuebyDJVbfjEpIWrpHSeX8GYyXhw" # <- ЗАМЕНИТЕ НА СВОЙ ТОКЕН!
# Базовый URL без параметра week
BASE_SCHEDULE_URL = "https://is.suvc.ru/blocks/manage_groups/website/view.php?gr=839&dep=3"
CACHE_FILE = "schedule_cache.txt"
SUBSCRIBERS_FILE = "subscribers.json" # Файл для хранения списка подписчиков
CHECK_INTERVAL_SECONDS = 600  # 10 минут
# --- /Настройки ---

# Настройка логирования для отладки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции для работы с подписчиками ---
def load_subscribers():
    """Загружает список подписчиков из файла."""
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f)) # Используем set для уникальности
        except (json.JSONDecodeError, FileNotFoundError):
            logger.warning(f"Файл {SUBSCRIBERS_FILE} поврежден или не найден, создаем новый список.")
            return set()
    return set()

def save_subscribers(subscribers):
    """Сохраняет список подписчиков в файл."""
    with open(SUBSCRIBERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(subscribers), f) # Сохраняем как список

# --- /Функции для работы с подписчиками ---

# Словарь для перевода английских дней недели на русские (остаётся для совместимости, если сайт вернётся к англ.)
EN_TO_RU_DAY_MAP = {
    "Monday": "Понедельник",
    "Tuesday": "Вторник",
    "Wednesday": "Среда",
    "Thursday": "Четверг",
    "Friday": "Пятница",
    "Saturday": "Суббота",
    "Sunday": "Воскресенье",
}

# Словарь для перевода месяцев
MONTH_TRANSLATION = {
    "January": "Января", "February": "Февраля", "March": "Марта", "April": "Апреля",
    "May": "Мая", "June": "Июня", "July": "Июля", "August": "Августа",
    "September": "Сентября", "October": "Октября", "November": "Ноября", "December": "Декабря"
}

def translate_day_of_week(english_day):
    """Переводит английское название дня недели на русское."""
    # Если день уже на русском, возвращаем его
    if any(ru_day in english_day for ru_day in EN_TO_RU_DAY_MAP.values()):
        return english_day
    # Иначе ищем перевод
    return EN_TO_RU_DAY_MAP.get(english_day, english_day) # Если перевод не найден, возвращаем оригинальное название

def calculate_week_number(target_date):
    """
    Вычисляет номер недели, считая, что учебный год начинается 1 сентября.
    target_date: объект datetime
    Возвращает: int (номер недели)
    """
    # Определяем год начала учебного года
    start_year = target_date.year
    if target_date.month < 9: # Если текущий месяц раньше сентября, значит, начало в прошлом году
        start_year = target_date.year - 1

    # Создаём дату начала учебного года
    start_of_academic_year = datetime(start_year, 9, 1)

    # Находим разницу в днях
    delta_days = (target_date.date() - start_of_academic_year.date()).days

    # Номер недели = (дни с начала года) // 7 + 1
    # (// - целочисленное деление)
    week_num = (delta_days // 7) + 1

    # logger.info(f"Дата {target_date.strftime('%Y-%m-%d')}, начало года {start_of_academic_year.date()}, номер недели: {week_num}") # ВРЕМЕННО
    return week_num


def parse_lessons_from_raw_text(raw_day_text):
    """
    Парсит "сырой" текст одного дня, извлекая дату, день недели и предметы с временем, преподавателем и кабинетом.
    Возвращает кортеж (день_недели_по_русски, дата, список_предметов).
    """
    # logger.info(f"Парсинг текста: {repr(raw_day_text)}") # ВРЕМЕННО для отладки
    if not raw_day_text:
        return None, None, []

    lines = raw_day_text.split('\n')
    if not lines:
        return None, None, []

    # Первая строка - это день недели и дата
    first_line = lines[0].strip()
    # Используем регулярное выражение для извлечения дня недели и даты
    # Теперь учитываем кириллицу: [а-яА-ЯёЁ]
    date_day_match = re.match(r'^(\d{1,2})\s+(\w+),\s+([а-яА-ЯёЁ]+)$', first_line, re.IGNORECASE)
    if not date_day_match:
        logger.error(f"Не удалось распознать день недели и дату (кириллица): {first_line}")
        # Попробуем снова с английским паттерном на всякий случай
        date_day_match = re.match(r'^(\d{1,2})\s+(\w+),\s+(.*)$', first_line)
        if not date_day_match:
             logger.error(f"Не удалось распознать день недели и дату (латиница): {first_line}")
             return None, None, []

    day_number = date_day_match.group(1)
    month = date_day_match.group(2)
    day_of_week_ru = date_day_match.group(3) # Получаем русское название

    # Теперь ищем пары построчно
    # Структура в raw_text:
    # 29 Сентября, Понедельник
    # 1 (номер пары)
    # 8-30 (время начала)
    # 10-10 (время конца)
    # Выбор ПО (предмет)
    # замена (опционально)
    # Николенко М.О. (преподаватель)
    # 21402 (аудитория)
    # 2 (номер следующей пары)
    # ...

    lessons = []
    i = 1  # Начинаем с 1, так как 0 - это заголовок дня

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Проверяем, является ли строка номером пары (цифра)
        if re.match(r'^\d+$', line):
            lesson_num = line
            i += 1
            start_time = ""
            end_time = ""
            subject = ""
            teacher_lines = []
            room_lines = []

            # Ожидаем время начала (XX-XX)
            if i < len(lines):
                time_start_line = lines[i].strip()
                if re.match(r'^\d+-\d+$', time_start_line):
                    start_time = time_start_line.replace('-', ':')
                    i += 1
                else:
                    # Если следующая строка не время, пропускаем номер пары и идём дальше
                    logger.warning(f"Ожидаем время начала после номера пары {lesson_num}, получили: {time_start_line}")
                    continue # Переходим к следующей итерации внешнего цикла

            # Ожидаем время конца (XX-XX)
            if i < len(lines):
                time_end_line = lines[i].strip()
                if re.match(r'^\d+-\d+$', time_end_line):
                    end_time = time_end_line.replace('-', ':')
                    i += 1
                else:
                    # Если следующая строка не время конца, пропускаем
                    logger.warning(f"Ожидаем время конца после времени начала для пары {lesson_num}, получили: {time_end_line}")
                    continue # Переходим к следующей итерации внешнего цикла

            # Ожидаем название предмета
            if i < len(lines):
                subject = lines[i].strip()
                i += 1

            # Теперь собираем "замена", преподавателей и кабинеты
            while i < len(lines):
                next_line = lines[i].strip()
                # Проверяем, не является ли это началом новой пары или днём недели
                if re.match(r'^\d+$', next_line) or re.match(r'^\d{1,2}\s+\w+,\s+(?:[а-яА-ЯёЁ]+|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))', next_line, re.IGNORECASE):
                    break # Конец текущей пары

                # Пропускаем строку "замена"
                if next_line.lower() == 'замена':
                    i += 1
                    continue

                # Проверяем, является ли строка кабинетом (только цифры)
                if re.match(r'^\d+$', next_line):
                    room_lines.append(next_line)
                    i += 1
                # Иначе, считаем, что это преподаватель
                else:
                    teacher_lines.append(next_line)
                    i += 1

            # Формируем строку времени
            time_str = f"{start_time}-{end_time}" if start_time and end_time else "Время не указано"

            # Форматируем строки препода и кабинета
            teacher_str = f" ({', '.join(teacher_lines)})" if teacher_lines else ""
            room_str = f" ауд. {', '.join(room_lines)}" if room_lines else ""

            # Добавляем предмет в список
            lessons.append(f"{lesson_num}. {subject} ({time_str}){teacher_str}{room_str}")

        else:
            # Если строка не начинается с номера пары, пропускаем её (например, если после последней пары есть мусор)
            i += 1

    return day_of_week_ru, f"{day_number} {month}", lessons


async def get_full_schedule_text(target_date):
    """
    Получает HTML-код страницы для нужной недели и извлекает ТЕКСТ расписания для всех дней недели.
    target_date: объект datetime, дата, для которой нужно расписание (используется для вычисления недели).
    """
    try:
        # Вычисляем номер недели для целевой даты
        week_number = calculate_week_number(target_date)

        # Формируем URL с параметром week
        schedule_url = f"{BASE_SCHEDULE_URL}&week={week_number}"

        logger.info(f"Запрашиваем расписание для недели {week_number} по URL: {schedule_url}")

        response = requests.get(schedule_url)
        response.raise_for_status()  # Проверяет, успешен ли запрос
        soup = BeautifulSoup(response.content, 'html.parser')

        # Извлекаем весь текст из тела страницы
        body_text = soup.get_text(separator='\n', strip=True)

        # logger.info(f"Полный текст страницы: {repr(body_text)}") # ВРЕМЕННО для отладки

        # --- ИЩЕМ РУССКИЕ ДНИ НЕДЕЛИ СНАЧАЛА ---
        day_pattern_ru = re.compile(r'(\d{1,2} \w+, (?:Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье))', re.IGNORECASE)
        all_day_matches_ru = day_pattern_ru.findall(body_text)
        all_day_positions_ru = [body_text.find(day) for day in all_day_matches_ru]

        if all_day_matches_ru:
            logger.info(f"Найдены дни с русским названием: {all_day_matches_ru}")
            all_day_matches = all_day_matches_ru
            all_day_positions = all_day_positions_ru
        else:
            # Если русские не найдены, ищем английские
            logger.error("Не найдены дни недели в тексте (поиск по русским названиям).")
            logger.info("Пробуем найти английские дни недели.")
            day_pattern_en = re.compile(r'(\d{1,2} \w+, (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))', re.IGNORECASE)
            all_day_matches_en = day_pattern_en.findall(body_text)
            all_day_positions_en = [body_text.find(day) for day in all_day_matches_en]
            if all_day_matches_en:
                logger.info(f"Найдены дни с английским названием: {all_day_matches_en}")
                all_day_matches = all_day_matches_en
                all_day_positions = all_day_positions_en
            else:
                logger.error("Не найдены дни недели ни с русскими, ни с английскими названиями.")
                return None, None


        logger.info(f"Найдены дни: {all_day_matches}") # ВРЕМЕННО для отладки

        schedule_dict = {}
        # ВАЖНО: Проходим по всем позициям, включая конец строки для последнего дня
        for i in range(len(all_day_positions)):
            day_name = all_day_matches[i]
            # Найдем начало текущего дня
            start_pos = all_day_positions[i]
            # Найдем конец текущего дня - это начало следующего или конец строки
            if i + 1 < len(all_day_positions):
                end_pos = all_day_positions[i + 1]
            else:
                end_pos = len(body_text)

            # Извлекаем текст текущего дня недели
            # Убираем лишние символы в начале и конце, но оставляем \n для разбиения на строки
            day_schedule_text = body_text[start_pos:end_pos].strip('\n\t ')
            schedule_dict[day_name] = day_schedule_text
            logger.info(f"Извлечённый ТЕКСТ для дня '{day_name}': {repr(day_schedule_text)}") # ВРЕМЕННО для отладки

        return schedule_dict, all_day_matches

    except requests.RequestException as e:
        logger.error(f"Ошибка при запросе к сайту: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Ошибка при парсинге HTML: {e}")
        return None, None

async def check_and_send_schedule_to_all(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет расписание и отправляет его всем подписчикам, если оно изменилось."""
    # Используем сегодняшнюю дату для проверки изменений
    today = datetime.today()
    current_schedule_dict, current_day_list = await get_full_schedule_text(today)

    if current_schedule_dict is None or current_day_list is None:
        logger.warning("Не удалось получить расписание с сайта (автоматическая проверка).")
        return

    # Считываем последнее сохраненное расписание из файла (в виде строки)
    last_schedule = ""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            last_schedule = f.read()

    # Преобразуем текущий словарь в строку для сравнения и сохранения
    # Для кэширования используем *сырой* текст, чтобы точнее отследить изменения
    current_schedule_str = "\n---\n".join([f"{day}: {details}" for day, details in current_schedule_dict.items()])

    # Проверяем, изменилось ли расписание
    if current_schedule_str != last_schedule:
        # Если изменилось, обновляем файл
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                f.write(current_schedule_str)

            # Загружаем список подписчиков
            subscribers = load_subscribers()

            if not subscribers:
                logger.info("Нет подписчиков для отправки уведомления.")
                return

            # Форматируем сообщение для отправки (например, только первый день)
            # Извлекаем данные для первого дня с помощью parse_lessons_from_raw_text
            first_day_key = current_day_list[0] if current_day_list else "День"
            raw_day_schedule = current_schedule_dict.get(first_day_key, "")
            day_of_week, date, lessons = parse_lessons_from_raw_text(raw_day_schedule)

            if not day_of_week or not date:
                 logger.warning(f"Не удалось распознать день недели или дату для {first_day_key}. Отправляем уведомление без деталей.")
                 message = f"<b>Расписание изменилось!</b>"
            else:
                formatted_lessons = "\n\n".join(lessons) # <- ИЗМЕНЕНО: Добавлен пробел между парами
                message = f"<b>Расписание изменилось!</b>\n\n<b>{html.escape(day_of_week)}, {html.escape(date)}</b>\n{html.escape(formatted_lessons)}"

            failed_sends = 0
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
                    logger.info(f"Уведомление об изменении расписания отправлено пользователю {chat_id}")
                except Exception as e:
                    logger.error(f"Не удалось отправить сообщение пользователю {chat_id}: {e}")
                    failed_sends += 1

            if failed_sends > 0:
                logger.info(f"Не удалось отправить {failed_sends} пользователям. Проверьте список подписчиков.")

            logger.info("Расписание обновлено и отправлено всем подписчикам (уведомление).")
        except Exception as e:
            logger.error(f"Ошибка при сохранении файла или отправке сообщений: {e}")
    else:
        logger.info("Расписание не изменилось (автоматическая проверка).")

async def check_and_send_schedule_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет расписание и отправляет ОТФОРМАТИРОВАННОЕ ВСЁ текущее пользователю, вызвавшему команду."""
    chat_id = update.effective_message.chat_id

    # Используем сегодняшнюю дату для получения полного расписания на текущую неделю
    today = datetime.today()
    current_schedule_dict, current_day_list = await get_full_schedule_text(today)

    if current_schedule_dict is None or current_day_list is None:
        await context.bot.send_message(chat_id=chat_id, text="Не удалось получить расписание с сайта.")
        return

    # Форматируем ВСЁ расписание
    full_schedule_message = "<b>Полное расписание:</b>\n\n"
    for day_name, raw_day_schedule in current_schedule_dict.items():
        day_of_week, date, lessons = parse_lessons_from_raw_text(raw_day_schedule)
        if day_of_week and date:
            formatted_lessons = "\n\n".join(lessons) # <- ИЗМЕНЕНО: Добавлен пробел между парами
            # Проверяем, есть ли у этого дня предметы
            if formatted_lessons.strip():
                 full_schedule_message += f"<b>{html.escape(day_of_week)}, {html.escape(date)}</b>\n{html.escape(formatted_lessons)}\n\n"
            else:
                 full_schedule_message += f"<b>{html.escape(day_of_week)}, {html.escape(date)}</b>\n(Пар нет)\n\n"
        else:
            # Если не удалось распознать день, отправим хотя бы "сырой" текст для отладки или как есть
            full_schedule_message += f"<b>Неизвестный день:</b>\n{html.escape(raw_day_schedule)}\n\n"

    try:
        await context.bot.send_message(chat_id=chat_id, text=full_schedule_message, parse_mode='HTML')
        logger.info(f"Отформатированное полное расписание отправлено пользователю {chat_id} по команде /check.")
    except Exception as e:
        logger.error(f"Ошибка при отправке отформатированного полного расписания пользователю {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")

async def send_today_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет расписание и отправляет ОТФОРМАТИРОВАННЫЙ первый день текущего пользователю, вызвавшему команду."""
    chat_id = update.effective_message.chat_id

    # --- НОВАЯ ЛОГИКА: ПОКАЗАТЬ ЗАВТРА ---
    # Получаем завтрашнюю дату
    tomorrow = datetime.today() + timedelta(days=1)
    tomorrow_day_num = tomorrow.day
    tomorrow_month_eng = tomorrow.strftime('%B') # Получаем английское название месяца
    tomorrow_month_ru = MONTH_TRANSLATION.get(tomorrow_month_eng, tomorrow_month_eng) # Переводим
    tomorrow_day_of_week_eng = tomorrow.strftime('%A') # Получаем английское название дня недели
    tomorrow_day_of_week_ru = translate_day_of_week(tomorrow_day_of_week_eng) # Переводим

    # Получаем расписание для недели, в которую входит завтрашний день
    current_schedule_dict, current_day_list = await get_full_schedule_text(tomorrow)

    if current_schedule_dict is None or current_day_list is None:
        await context.bot.send_message(chat_id=chat_id, text="Не удалось получить расписание с сайта.")
        return

    # Ищем в расписании день, соответствующий завтрашней дате
    target_day_key = None
    for day_key in current_day_list:
         # day_key выглядит как "29 September, Monday"
         # Проверим, совпадают ли день и месяц
         if str(tomorrow_day_num) in day_key and tomorrow_month_ru in day_key:
             target_day_key = day_key
             break

    if not target_day_key:
        # Если день не найден в расписании (например, выходной или нет пар), отправим сообщение
        message = f"<b>Расписание на {html.escape(tomorrow_day_of_week_ru)}, {tomorrow_day_num} {html.escape(tomorrow_month_ru)}:</b>\n\n(Пар нет или расписание отсутствует)"
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
            logger.info(f"Расписание на завтра (не найдено в списке) отправлено пользователю {chat_id} по команде /today.")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения 'Пар нет' пользователю {chat_id}: {e}")
        return

    raw_day_schedule = current_schedule_dict.get(target_day_key, "")
    day_of_week, date, lessons = parse_lessons_from_raw_text(raw_day_schedule)

    if not day_of_week or not date:
         message = f"<b>Расписание на ближайший день:</b>\n\nНе удалось распознать день недели или дату.\n{html.escape(raw_day_schedule)}"
    else:
        formatted_lessons = "\n\n".join(lessons) # <- ИЗМЕНЕНО: Добавлен пробел между парами
        # Проверяем, есть ли у этого дня предметы
        if formatted_lessons.strip():
             message = f"<b>Расписание на {html.escape(day_of_week)}, {html.escape(date)}:</b>\n\n{html.escape(formatted_lessons)}"
        else:
             message = f"<b>Расписание на {html.escape(day_of_week)}, {html.escape(date)}:</b>\n\n(Пар нет)"

    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
        logger.info(f"Отформатированное расписание на завтра (или ближайший день с парами) отправлено пользователю {chat_id} по команде /today.")
    except Exception as e:
        logger.error(f"Ошибка при отправке отформатированного расписания на завтра пользователю {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")

async def send_schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет клавиатуру с кнопками дней недели."""
    chat_id = update.effective_message.chat_id

    # Получаем расписание на *текущую* неделю (для меню выбора)
    # Используем сегодняшнюю дату, чтобы получить текущую неделю
    today = datetime.today()
    _, current_day_list = await get_full_schedule_text(today)

    if not current_day_list:
        await context.bot.send_message(chat_id=chat_id, text="Не удалось получить дни недели с сайта.")
        return

    # Создаём кнопки
    keyboard = []
    for day in current_day_list:
        # Извлекаем день недели из строки типа "29 Сентября, Понедельник"
        # Это нужно для корректного отображения кнопки и callback_data
        # Теперь ищем русские дни недели
        day_match = re.search(r'(?:Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье)', day, re.IGNORECASE)
        if day_match:
            russian_day = day_match.group()
            # Используем русское название для отображения и callback_data
            keyboard.append([InlineKeyboardButton(russian_day, callback_data=day)])
        else:
            # Если не удалось извлечь день недели, используем строку как есть
            keyboard.append([InlineKeyboardButton(day, callback_data=day)])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(chat_id=chat_id, text="Выберите день недели:", reply_markup=reply_markup)

async def handle_day_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие на кнопку дня недели."""
    query = update.callback_query
    await query.answer() # Отвечаем на callback, чтобы убрать "часики"

    selected_day = query.data # Это строка типа "29 Сентября, Понедельник"
    chat_id = query.message.chat_id

    # --- НОВАЯ ЛОГИКА: ОПРЕДЕЛИТЬ НЕДЕЛЮ ДЛЯ ВЫБРАННОГО ДНЯ ---
    # Извлечём дату из строки selected_day
    # Например, "29 Сентября, Понедельник"
    # Нужно сопоставить "Сентябрь" с английским "September" и "Понедельник" с "Monday"
    # Это хрупко, но можно попробовать.
    # Сначала разобьём строку
    parts = selected_day.split(', ')
    if len(parts) < 2:
         logger.error(f"Не удалось разобрать строку дня: {selected_day}")
         await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")
         return

    date_part = parts[0] # "29 Сентября"
    day_of_week_part = parts[1] # "Понедельник"

    # Разобьём дату
    date_parts = date_part.split(' ')
    if len(date_parts) < 2:
         logger.error(f"Не удалось разобрать часть даты: {date_part}")
         await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")
         return

    day_num_str = date_parts[0] # "29"
    month_ru = date_parts[1] # "Сентября"

    # Найдём английский месяц
    eng_month = next((eng, ru) for eng, ru in MONTH_TRANSLATION.items() if ru == month_ru)
    if not eng_month:
        logger.error(f"Не удалось найти английский месяц для: {month_ru}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")
        return
    eng_month = eng_month[0] # Получаем ключ (английский месяц)

    # Найдём английский день недели
    eng_day_of_week = next((eng, ru) for eng, ru in EN_TO_RU_DAY_MAP.items() if ru == day_of_week_part)
    if not eng_day_of_week:
        logger.error(f"Не удалось найти английский день недели для: {day_of_week_part}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")
        return
    eng_day_of_week = eng_day_of_week[0] # Получаем ключ (английский день недели)

    # Попробуем создать приблизительную дату, чтобы вычислить неделю.
    # Так как год не указан, возьмём текущий год и попытаемся подобрать.
    current_year = datetime.now().year
    # Создаём строку даты в формате, понятном datetime
    # Используем ближайший год, в котором этот день недели совпадает с днём месяца
    found_date = None
    for year_offset in [0, -1, 1]: # Проверим текущий, прошлый и следующий год
        try:
            test_date = datetime.strptime(f"{day_num_str} {eng_month} {current_year + year_offset}", "%d %B %Y")
            if test_date.strftime('%A') == eng_day_of_week:
                 found_date = test_date
                 break
        except ValueError:
            continue

    if not found_date:
        logger.error(f"Не удалось определить дату для {selected_day}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")
        return

    # Теперь у нас есть дата для выбранного дня
    # Получаем расписание для недели, в которую входит этот день
    current_schedule_dict, _ = await get_full_schedule_text(found_date)

    if current_schedule_dict is None:
        await context.bot.send_message(chat_id=chat_id, text="Не удалось получить расписание с сайта.")
        return

    # Ищем расписание для выбранного дня (ключа словаря)
    raw_day_schedule = current_schedule_dict.get(selected_day, "")

    day_of_week, date, lessons = parse_lessons_from_raw_text(raw_day_schedule)

    if not day_of_week or not date:
         message = f"<b>Расписание на {html.escape(selected_day)}:</b>\n\nНе удалось распознать день недели или дату.\n{html.escape(raw_day_schedule)}"
    else:
        formatted_lessons = "\n\n".join(lessons) # <- ИЗМЕНЕНО: Добавлен пробел между парами
        # Проверяем, есть ли у этого дня предметы
        if formatted_lessons.strip():
             message = f"<b>Расписание на {html.escape(day_of_week)}, {html.escape(date)}:</b>\n\n{html.escape(formatted_lessons)}"
        else:
             message = f"<b>Расписание на {html.escape(day_of_week)}, {html.escape(date)}:</b>\n\n(Пар нет)"

    try:
        # Редактируем сообщение с меню, чтобы показать расписание
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text=message,
            parse_mode='HTML'
        )
        logger.info(f"Отформатированное расписание на {selected_day} отправлено пользователю {chat_id} по нажатию кнопки.")
    except Exception as e:
        logger.error(f"Ошибка при отправке отформатированного расписания на {selected_day} пользователю {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при получении расписания.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветственное сообщение."""
    welcome_message = (
        'Привет! Я бот, который проверяет расписание.\n\n'
        'Команды:\n'
        '/start - Показать это сообщение\n'
        '/subscribe - Подписаться на уведомления об изменениях\n'
        '/unsubscribe - Отписаться от уведомлений\n'
        '/check - Проверить ПОЛНОЕ расписание\n'
        '/today - Проверить расписание на ЗАВТРА\n' # <- ИЗМЕНЕНО
        '/schedule - Показать меню выбора дня недели'
    )
    await update.message.reply_text(welcome_message)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет пользователя в список подписчиков."""
    chat_id = update.effective_message.chat_id
    subscribers = load_subscribers()

    if chat_id in subscribers:
        await update.message.reply_text("Вы уже подписаны на уведомления.")
    else:
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("Вы успешно подписались на уведомления об изменениях расписания!")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет пользователя из списка подписчиков."""
    chat_id = update.effective_message.chat_id
    subscribers = load_subscribers()

    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("Вы успешно отписались от уведомлений.")
    else:
        await update.message.reply_text("Вы не были подписаны.")

def main():
    """Запускает бота."""
    # Создаем приложение бота
    application = Application.builder().token(BOT_TOKEN).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check_and_send_schedule_manual))
    application.add_handler(CommandHandler("today", send_today_schedule))
    application.add_handler(CommandHandler("schedule", send_schedule_menu))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))

    # Добавляем обработчик нажатий на кнопки
    application.add_handler(CallbackQueryHandler(handle_day_selection))

    # Запускаем фоновую задачу для автоматической проверки
    # first=10 означает, что первая проверка произойдет через 10 секунд после запуска
    application.job_queue.run_repeating(
        callback=check_and_send_schedule_to_all,
        interval=CHECK_INTERVAL_SECONDS,
        first=10
    )

    # Запускаем бота
    logger.info("Бот запущен.")
    application.run_polling()

if __name__ == '__main__':
    main()