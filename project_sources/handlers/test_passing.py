from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
from tools.config import ADMIN_CHAT_ID
from aiogram import Router, types, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from tools.models import Test, Question, TestAttempt, User
import logging
from aiogram.exceptions import TelegramBadRequest

from tools.states import TestStates  # Импортируем TestStates из states.py
from utils.decorators import check_active_test  # Импортируем декоратор
from utils.calculate_score import calculate_score  # Импортируем функцию расчёта баллов

import asyncio

router = Router()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


def current_time():
    return datetime.now(ZoneInfo("Europe/Moscow"))


async def notify_admin(bot: Bot, message: str):
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=message)
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора: {e}")


async def monitor_test_time(user_id: int, test_id: int, end_time: datetime, session: AsyncSession, bot: Bot):
    now = current_time()
    delay = (end_time - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    test_attempt_result = await session.execute(
        select(TestAttempt).where(TestAttempt.test_id == test_id, TestAttempt.user_id == user_id)
    )
    test_attempt: Optional[TestAttempt] = test_attempt_result.scalars().first()

    if test_attempt and not test_attempt.passed:
        test_result = await session.execute(select(Test).where(Test.id == test_id))
        test: Optional[Test] = test_result.scalars().first()
        if not test:
            logger.error(f"Тест с ID {test_id} не найден при мониторинге времени.")
            return

        questions_result = await session.execute(
            select(Question).where(Question.test_id == test_id)
        )
        questions = questions_result.scalars().all()

        answers = test_attempt.answers if test_attempt.answers else {}
        score, passed, detailed_answers = calculate_score(test, answers, questions)

        test_attempt.score = score
        test_attempt.passed = passed
        test_attempt.end_time = end_time.replace(tzinfo=None)
        test_attempt.answers = detailed_answers  # Обновляем ответы с информацией о правильности

        try:
            await session.commit()
            await bot.send_message(
                chat_id=user_id,
                text="Время теста истекло. Ваш тест завершён.\n\n" +
                     f"**Баллы:** {score}\n" +
                     f"**Статус:** {'✅ Пройден' if passed else '❌ Не пройден'}",
                parse_mode='Markdown'
            )
            logger.info(
                f"Автоматически завершён тест {test_id} для пользователя {user_id} с баллом {score} и статусом {'пройден' if passed else 'не пройден'}.")
        except Exception as e:
            await session.rollback()
            logger.error(f"Ошибка при автоматическом завершении теста: {e}")
            await notify_admin(bot, f"Ошибка при автоматическом завершении теста: {e}")


@router.callback_query(lambda c: c.data and c.data.startswith("select_test:"))
async def start_test(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    await callback.answer()

    test_id_str = callback.data.split(":")[1]
    if not test_id_str.isdigit():
        await callback.message.answer("Некорректный ID теста.")
        return

    test_id = int(test_id_str)
    test_result = await session.execute(select(Test).where(Test.id == test_id))
    test: Optional[Test] = test_result.scalars().first()

    if not test:
        await callback.message.answer("Тест не найден.")
        return

    question_result = await session.execute(select(Question).where(Question.test_id == test_id))
    questions = question_result.scalars().all()

    if not questions:
        await callback.message.answer("В этом тесте пока нет вопросов.")
        return

    start_time = current_time()
    end_time = start_time + timedelta(minutes=test.duration)

    await state.update_data(
        test_id=test_id,
        questions=questions,
        current_index=0,
        start_time=start_time,
        end_time=end_time,
        answers={},
        message_id=callback.message.message_id
    )
    await state.set_state(TestStates.TESTING.state)

    await send_question(callback.message, state)

    asyncio.create_task(
        monitor_test_time(user_id=callback.from_user.id, test_id=test_id, end_time=end_time, session=session, bot=bot))


@router.callback_query(lambda c: c.data and c.data.startswith("answer:"))
@check_active_test
async def handle_answer(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession):
    await callback.answer()

    user_data = await state.get_data()
    logger.debug(f"Handle Answer - Current FSM data: {user_data}")

    test_data = user_data
    current_index = test_data["current_index"]
    questions = test_data["questions"]
    current_question: Question = questions[current_index]

    answer_id_str = callback.data.split(":")[1]
    if not answer_id_str.isdigit():
        await callback.message.answer("Некорректный ID ответа.")
        return

    answer_id = int(answer_id_str)

    if current_question.question_type == "single_choice":
        current_selected = test_data["answers"].get(str(current_question.id))
        if current_selected == answer_id_str:
            test_data["answers"].pop(str(current_question.id), None)
            logger.info(
                f"Пользователь {callback.from_user.id} снял выбор с ответа {answer_id_str} для вопроса {current_question.id}")
        else:
            test_data["answers"][str(current_question.id)] = answer_id_str
            logger.info(
                f"Пользователь {callback.from_user.id} выбрал ответ {answer_id_str} для вопроса {current_question.id}")
    elif current_question.question_type == "multiple_choice":
        current_answer = test_data["answers"].get(str(current_question.id), "")
        if answer_id_str in current_answer:
            current_answer = current_answer.replace(answer_id_str, "")
            logger.info(
                f"Пользователь {callback.from_user.id} убрал выбор ответа {answer_id_str} для вопроса {current_question.id}")
        else:
            current_answer += answer_id_str
            logger.info(
                f"Пользователь {callback.from_user.id} добавил выбор ответа {answer_id_str} для вопроса {current_question.id}")
        # Сортируем и сохраняем ответ
        test_data["answers"][str(current_question.id)] = ''.join(sorted(current_answer))
    else:
        await callback.message.answer("Неподдерживаемый тип вопроса.")
        return

    await state.update_data(answers=test_data["answers"])
    await send_question(callback.message, state)


@router.callback_query(lambda c: c.data and c.data.startswith("navigate:"))
@check_active_test
async def navigate_question(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    user_data = await state.get_data()
    logger.debug(f"Navigate Question - Current FSM data: {user_data}")

    action = callback.data.split(":")[1]
    current_index = user_data["current_index"]
    questions = user_data["questions"]

    if action == "next" and current_index < len(questions) - 1:
        current_index += 1
    elif action == "prev" and current_index > 0:
        current_index -= 1
    else:
        await callback.message.answer("Невозможно выполнить это действие.")
        return

    await state.update_data(current_index=current_index)
    await send_question(callback.message, state)


@router.callback_query(lambda c: c.data and c.data.startswith("edit_answer:"))
@check_active_test
async def edit_answer(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession):
    await callback.answer()

    user_data = await state.get_data()
    logger.debug(f"Edit Answer - Current FSM data: {user_data}")

    test_data = user_data
    current_index = test_data["current_index"]
    questions = test_data["questions"]
    current_question: Question = questions[current_index]

    if current_question.question_type != "text_input":
        await callback.message.answer("Этот вопрос не поддерживает текстовый ввод.")
        return

    existing_answer = test_data["answers"].get(str(current_question.id), "")
    await callback.message.answer(
        f"Введите новый ответ для вопроса:\n\n{current_question.question_text}\n\n"
        f"Текущий ответ: {existing_answer if existing_answer else 'Нет ответа'}",
        reply_markup=types.ReplyKeyboardRemove()
    )

    await state.set_state(TestStates.EDITING.state)
    await state.update_data(editing_question_id=current_question.id)


@router.message(TestStates.EDITING)
async def handle_text_edit(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    logger.debug(f"Handle Text Edit - Current FSM data: {user_data}")

    editing_question_id = user_data.get("editing_question_id")

    if not editing_question_id:
        await message.answer("Нет вопроса для редактирования.")
        return

    answers: Dict[str, Any] = user_data.get("answers", {})
    answers[str(editing_question_id)] = message.text
    logger.debug(f"Updated answers: {answers}")
    await state.update_data(answers=answers)

    await state.set_state(TestStates.TESTING.state)

    await message.answer("Ответ обновлен.", reply_markup=types.ReplyKeyboardRemove())
    await send_question(message, state)


@router.callback_query(lambda c: c.data == "finish_test")
@check_active_test
async def initiate_finish_test(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    user_data = await state.get_data()
    logger.debug(f"Initiate Finish Test - Current FSM data: {user_data}")

    await state.set_state(TestStates.CONFIRM_FINISH.state)

    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="confirm_finish_yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="confirm_finish_no")
        ]
    ])

    confirmation_msg = await callback.message.answer(
        "Вы уверены, что хотите завершить тестирование?",
        reply_markup=confirm_keyboard
    )
    await state.update_data(confirmation_message_id=confirmation_msg.message_id)


@router.callback_query(lambda c: c.data == "confirm_finish_yes")
@check_active_test
async def confirm_finish_yes(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    await callback.answer()

    current_state = await state.get_state()
    if current_state != TestStates.CONFIRM_FINISH.state:
        await callback.message.answer("Ваше тестирование не активно.")
        return

    user_data = await state.get_data()
    logger.debug(f"Confirm Finish Yes - Current FSM data: {user_data}")

    test_id = user_data.get("test_id")
    answers = user_data.get("answers", {})
    questions = user_data.get("questions", [])
    user_id = callback.from_user.id
    start_time = user_data.get("start_time", current_time())
    end_time = current_time()

    test_result = await session.execute(select(Test).where(Test.id == test_id))
    test: Optional[Test] = test_result.scalars().first()
    if not test:
        await callback.message.answer("Тест не найден.")
        return

    user_result = await session.execute(select(User).where(User.user_id == user_id))
    user: Optional[User] = user_result.scalars().first()
    if not user:
        await callback.message.answer("Пользователь не найден в системе.")
        return

    score, passed, detailed_answers = calculate_score(test, answers, questions)

    test_attempt = TestAttempt(
        test_id=test_id,
        user_id=user.id,
        start_time=start_time.replace(tzinfo=None),
        end_time=end_time.replace(tzinfo=None),
        score=score,
        passed=passed,
        answers=detailed_answers
    )

    try:
        session.add(test_attempt)
        await session.commit()
        await callback.message.answer(
            "Вы успешно завершили тест. Спасибо за участие!\n\n" +
            f"**Баллы:** {score}\n" +
            f"**Статус:** {'✅ Пройден' if passed else '❌ Не пройден'}",
            parse_mode='Markdown'
        )
        logger.info(
            f"Пользователь {user_id} завершил тест {test_id} с баллом {score} и статусом {'пройден' if passed else 'не пройден'}")
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка при сохранении результатов теста: {e}")
        await callback.message.answer("Произошла ошибка при сохранении результатов теста. Попробуйте позже.")
        await notify_admin(bot, f"Ошибка при сохранении результатов теста: {e}")
        return

    await state.clear()

    disabled_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Завершить тест (недоступно)", callback_data="noop")
        ]
    ])

    try:
        await callback.message.edit_reply_markup(reply_markup=disabled_keyboard)
        logger.debug("Test finish buttons disabled successfully.")
    except TelegramBadRequest as e:
        logger.error(f"Ошибка при редактировании кнопок после завершения теста: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при редактировании кнопок: {e}")


@router.callback_query(lambda c: c.data == "confirm_finish_no")
@check_active_test
async def confirm_finish_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    user_data = await state.get_data()
    logger.debug(f"Confirm Finish No - Current FSM data: {user_data}")

    confirmation_message_id = user_data.get("confirmation_message_id")

    if confirmation_message_id:
        disabled_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да (недоступно)", callback_data="noop"),
                InlineKeyboardButton(text="❌ Нет (завершение отменено)", callback_data="noop")
            ]
        ])

        try:
            await callback.message.bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id,
                message_id=confirmation_message_id,
                reply_markup=disabled_keyboard
            )
            logger.debug("Confirmation message buttons disabled successfully.")
        except TelegramBadRequest as e:
            logger.error(f"Ошибка при редактировании сообщения подтверждения: {e}")

        await state.update_data(confirmation_message_id=None)

    await state.set_state(TestStates.TESTING.state)

    await callback.message.answer("Продолжаем тестирование.")
    await send_question(callback.message, state)


@router.callback_query(lambda c: c.data == "noop")
async def noop_handler(callback: types.CallbackQuery):
    await callback.answer()


async def send_question(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    logger.debug(f"Send Question - Current FSM data: {user_data}")

    if not user_data or 'test_id' not in user_data:
        await message.answer("Ваше тестирование не активно.")
        return

    questions: list[Question] = user_data["questions"]
    current_index: int = user_data["current_index"]
    current_question: Question = questions[current_index]
    answers: Dict[str, Any] = user_data.get("answers", {})

    question_text = (
        f"Вопрос {current_index + 1}/{len(questions)}:\n\n"
        f"{current_question.question_text}\n\n"
    )

    if current_question.question_type == "text_input":
        current_answer = answers.get(str(current_question.id), "")
        question_text += f"Текущий ответ: {current_answer if current_answer else 'Нет ответа'}\n\n"
    else:
        question_text += "Тип вопроса: " + (
            "множественный выбор" if current_question.question_type == "multiple_choice"
            else "одиночный выбор" if current_question.question_type == "single_choice"
            else "неизвестный тип"
        ) + "\n\n"

        for idx, option in enumerate(current_question.options, start=1):
            if current_question.question_type in ["single_choice", "multiple_choice"]:
                if current_question.question_type == "single_choice":
                    is_selected = (str(option["id"]) == str(answers.get(str(current_question.id), "")))
                else:
                    is_selected = (str(option["id"]) in str(answers.get(str(current_question.id), "")))

                checkmark = "✅" if is_selected else ""
                question_text += f"{idx}. {option['text']} {checkmark}\n"
            else:
                question_text += f"{idx}. {option['text']}\n"

    buttons = []
    if current_question.question_type == "text_input":
        buttons.append([
            InlineKeyboardButton(
                text="Редактировать ответ",
                callback_data=f"edit_answer:{current_question.id}"
            )
        ])
    else:
        option_buttons = []
        for idx, option in enumerate(current_question.options, start=1):
            if current_question.question_type == "single_choice":
                is_selected = (str(option["id"]) == str(answers.get(str(current_question.id), "")))
            elif current_question.question_type == "multiple_choice":
                is_selected = (str(option["id"]) in str(answers.get(str(current_question.id), "")))
            else:
                is_selected = False

            checkmark = "✅" if is_selected else ""
            button_text = f"{idx} {checkmark}" if is_selected else f"{idx}"

            option_buttons.append(
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"answer:{option['id']}"
                )
            )
        buttons.append(option_buttons)

    navigation_buttons = []
    if current_index > 0:
        navigation_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="navigate:prev"))
    navigation_buttons.append(InlineKeyboardButton(text=f"{current_index + 1}/{len(questions)}", callback_data="noop"))
    if current_index < len(questions) - 1:
        navigation_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data="navigate:next"))

    navigation_buttons.append(InlineKeyboardButton(text="✅ Завершить тест", callback_data="finish_test"))

    if navigation_buttons:
        buttons.append(navigation_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    message_id = user_data.get("message_id")
    if message_id:
        try:
            await message.bot.edit_message_text(
                text=question_text,
                chat_id=message.chat.id,
                message_id=message_id,
                reply_markup=keyboard
            )
            logger.debug("Question message edited successfully.")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                logger.debug("Message is not modified. Skipping edit.")
                pass
            else:
                logger.error(f"Ошибка при редактировании сообщения: {e}")
                msg = await message.answer(question_text, reply_markup=keyboard)
                await state.update_data(message_id=msg.message_id)
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
            msg = await message.answer(question_text, reply_markup=keyboard)
            await state.update_data(message_id=msg.message_id)
    else:
        msg = await message.answer(question_text, reply_markup=keyboard)
        await state.update_data(message_id=msg.message_id)
