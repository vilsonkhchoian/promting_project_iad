import asyncio
import re
import time
import uuid
import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
import config

router = Router()
access_token = None
expires_at = 0


class EssayCheck(StatesGroup):
    waiting_task = State()
    waiting_essay = State()


def count_english_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", text))


def count_sentences(text: str) -> int:
    return len(re.findall(r"[.!?]+", text))


def english_letter_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if not letters:
        return 0.0

    english_letters = re.findall(r"[A-Za-z]", text)
    return len(english_letters) / len(letters)


def is_mostly_english(text: str) -> bool:
    min_ratio = 0.7
    return english_letter_ratio(text) >= min_ratio


def validate_task(task: str) -> tuple[bool, str | None]:
    min_length = 40
    max_length = 1500
    min_words = 6

    if len(task) < min_length:
        return False, "Задание выглядит слишком коротким. Пришлите полный текст задания."

    if len(task) > max_length:
        return False, "Задание слишком длинное. Пришлите более короткую формулировку задания."

    if count_english_words(task) < min_words or not is_mostly_english(task):
        return False, "Задание должно быть в основном на английском языке. Пришлите полную формулировку задания."

    return True, None


def validate_essay(essay: str) -> tuple[bool, str | None]:
    min_length = 600
    max_length = 12000
    min_words =  120
    min_sentences = 6

    if len(essay) < min_length:
        return False, "Ответ выглядит слишком коротким. Пришлите полный текст эссе."

    if len(essay) > max_length:
        return False, "Эссе слишком длинное. Сократите текст и отправьте снова."

    if count_english_words(essay) < min_words or not is_mostly_english(essay):
        return False, "Эссе должно быть в основном на английском языке. Пришлите полный текст эссе."

    if count_sentences(essay) < min_sentences:
        return False, "Ответ не похож на полноценное эссе. Пришлите текст эссе целиком."

    return True, None


def build_user_prompt(task: str, essay: str) -> str:
    return (config.PROMPT_3_USER_TEMPLATE.replace("{TASK}", task.strip()).replace("{ESSAY}", essay.strip()))


def split_long_message(text: str, limit: int = 4000) -> list[str]:
    parts = []
    text = text.strip()

    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        parts.append(text)

    return parts


async def get_access_token(session: aiohttp.ClientSession) -> str:
    global access_token, expires_at

    if access_token and time.time() < expires_at:
        return access_token

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {config.GIGACHAT_AUTH_KEY}",
    }

    timeout = aiohttp.ClientTimeout(total=60)
    ssl_context = False if not config.GIGACHAT_SSL_VERIFY else None

    async with session.post(
        config.GIGACHAT_AUTH_URL,
        headers=headers,
        data={"scope": config.GIGACHAT_SCOPE},
        timeout=timeout,
        ssl=ssl_context,
    ) as response:
        if response.status >= 400:
            response_text = await response.text()
            raise RuntimeError(f"Ошибка авторизации GigaChat: HTTP {response.status}: {response_text}")

        token_data = await response.json()

    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(f"GigaChat не вернул access_token: {token_data}")

    raw_expires_at = token_data.get("expires_at")
    if isinstance(raw_expires_at, (int, float)):
        expires_at = raw_expires_at / 1000 if raw_expires_at > 10000000000 else raw_expires_at
        expires_at = max(time.time(), expires_at - 60)
    else:
        expires_at = time.time() + 1500

    return access_token


async def ask_gigachat(session: aiohttp.ClientSession, task: str, essay: str) -> str:
    token = await get_access_token(session)
    prompt = build_user_prompt(task, essay)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "model": config.GIGACHAT_MODEL,
        "messages": [
            {"role": "system", "content": config.PROMPT_3_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
    }

    timeout = aiohttp.ClientTimeout(total=120)
    ssl_context = False if not config.GIGACHAT_SSL_VERIFY else None

    async with session.post(
        config.GIGACHAT_CHAT_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
        ssl=ssl_context,
    ) as response:
        if response.status >= 400:
            response_text = await response.text()
            raise RuntimeError(f"Ошибка запроса к GigaChat: HTTP {response.status}: {response_text}")

        result = await response.json()

    choices = result.get("choices")
    if not choices or not isinstance(choices, list):
        raise RuntimeError(f"Неожиданный ответ GigaChat: {result}")

    answer = choices[0].get("message", {}).get("content")
    if not answer:
        raise RuntimeError(f"Неожиданный ответ GigaChat: {result}")

    return answer.strip()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(EssayCheck.waiting_task)
    await message.answer(
        "Привет! Пришлите задание для эссе.\n\n"
        "После этого я попрошу прислать твой ответ и отправлю оценку с пояснениями."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, проверка отменена. Чтобы начать заново, отправьте /start.")


@router.message(EssayCheck.waiting_task, F.text)
async def get_task(message: Message, state: FSMContext):
    task = message.text.strip()
    is_valid, error = validate_task(task)

    if not is_valid:
        await message.answer(error)
        return

    await state.update_data(task=task)
    await state.set_state(EssayCheck.waiting_essay)
    await message.answer("Теперь пришлите свой ответ на это задание.")


@router.message(EssayCheck.waiting_essay, F.text)
async def get_essay_and_evaluate(message: Message, state: FSMContext):
    essay = message.text.strip()
    is_valid, error = validate_essay(essay)

    if not is_valid:
        await message.answer(error)
        return

    data = await state.get_data()
    task = data.get("task")

    if not task:
        await state.clear()
        await message.answer("Не нашел сохраненное задание. Начните заново командой /start.")
        return

    await message.answer("Проверяю эссе и готовлю оценку...")

    try:
        async with aiohttp.ClientSession() as session:
            answer = await ask_gigachat(session, task, essay)
    except Exception as error:
        await message.answer(str(error))
        return

    for part in split_long_message(answer):
        await message.answer(part)

    await state.clear()
    await message.answer("Готово. Для новой проверки отправьте /start.")


@router.message(F.text)
async def fallback(message: Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state is None:
        await message.answer("Чтобы начать проверку эссе, отправьте /start.")
    else:
        await message.answer("Пожалуйста, отправьте текстовое сообщение по текущему шагу или /cancel.")


async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
