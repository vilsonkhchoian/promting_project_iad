import asyncio
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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(EssayCheck.waiting_task)
    await message.answer("Привет! Пришлите задание для эссе.\n\nПосле этого я попрошу прислать твой ответ и отправлю оценку с пояснениями.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, проверка отменена. Чтобы начать заново, отправьте /start.")


@router.message(EssayCheck.waiting_task, F.text)
async def get_task(message: Message, state: FSMContext):
    task = message.text.strip()
    if len(task) < 10:
        await message.answer("Задание выглядит слишком коротким. Пришлите полный текст задания.")
        return
    await state.update_data(task=task)
    await state.set_state(EssayCheck.waiting_essay)
    await message.answer("Теперь пришлите свой ответ на это задание.")


@router.message(EssayCheck.waiting_essay, F.text)
async def get_essay_and_evaluate(message: Message, state: FSMContext):
    global access_token, expires_at
    essay = message.text.strip()
    if len(essay) < 20:
        await message.answer("Ответ выглядит слишком коротким. Пришлите полный текст эссе.")
        return
    data = await state.get_data()
    task = data.get("task")
    if not task:
        await state.clear()
        await message.answer("Не нашел сохраненное задание. Начните заново командой /start.")
        return
    await message.answer("Проверяю эссе и готовлю оценку...")
    prompt = config.PROMPT_3_TEMPLATE.replace("{TASK}", task.strip()).replace("{ESSAY}", essay.strip())
    async with aiohttp.ClientSession() as session:
        if not access_token or time.time() >= expires_at:
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "RqUID": str(uuid.uuid4()), "Authorization": f"Basic {config.GIGACHAT_AUTH_KEY}"}
            async with session.post(config.GIGACHAT_AUTH_URL, headers=headers, data={"scope": config.GIGACHAT_SCOPE}, timeout=aiohttp.ClientTimeout(total=60), ssl=False if not config.GIGACHAT_SSL_VERIFY else None) as response:
                if response.status >= 400:
                    await message.answer(f"Ошибка авторизации GigaChat: HTTP {response.status}: {await response.text()}")
                    return
                token_data = await response.json()
            access_token = token_data.get("access_token")
            if not access_token:
                await message.answer(f"GigaChat не вернул access_token: {token_data}")
                return
            raw_expires_at = token_data.get("expires_at")
            if isinstance(raw_expires_at, (int, float)):
                expires_at = raw_expires_at / 1000 if raw_expires_at > 10000000000 else raw_expires_at
                expires_at = max(time.time(), expires_at - 60)
            else:
                expires_at = time.time() + 1500
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"}
        payload = {"model": config.GIGACHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": config.TEMPERATURE, "max_tokens": config.MAX_TOKENS}
        async with session.post(config.GIGACHAT_CHAT_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120), ssl=False if not config.GIGACHAT_SSL_VERIFY else None) as response:
            if response.status >= 400:
                await message.answer(f"Ошибка запроса к GigaChat: HTTP {response.status}: {await response.text()}")
                return
            result = await response.json()
    choices = result.get("choices")
    if not choices or not isinstance(choices, list):
        await message.answer(f"Неожиданный ответ GigaChat: {result}")
        return
    answer = choices[0].get("message", {}).get("content")
    if not answer:
        await message.answer(f"Неожиданный ответ GigaChat: {result}")
        return
    answer = answer.strip()
    while len(answer) > 4000:
        split_at = answer.rfind("\n", 0, 4000)
        if split_at == -1:
            split_at = 4000
        await message.answer(answer[:split_at].strip())
        answer = answer[split_at:].strip()
    if answer:
        await message.answer(answer)
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