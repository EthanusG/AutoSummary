import discord
from discord.ext import commands
from openai import AsyncOpenAI
import asyncio
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YUNWU_API_KEY = os.getenv("YUNWU_API_KEY")

client = AsyncOpenAI(
    api_key = YUNWU_API_KEY,
    base_url = "https://yunwu.ai/v1"
)

MODEL_NAME = "gemini-3-flash-preview"

DEFAULT_PROMPT = (
    "**Your objective**: Summarize the provided texts.\n"
    "**Input**: Several parts of an online meeting transcript will be sent one at a time. "
    "They are all related to the same meeting.\n"
    "**Output**: Generate a concise summary (2-3 sentences) for the latest chunk of the transcript provided. "
    "Focus on the content of the discussion (e.g. What was the meeting about? What key points did a participant make? What were the reactions of others?). "
    "The summary must be mainly in **Chinese Simplified** (a few English words are tolerated). "
    "When a participant's name is included, prepend an '@' right before it, and leave spaces before the '@' and after the name (e.g. 'name' -> ' @name ')."
)

FINAL_EVAL_PROMPT = (
    "This is the end of the transcript! Please critically comment on each participant's person and performance "
    "during this meeting based on the entire conversation history above. "
    "Each comment should be around 2-3 sentences, in a new line. "
    "The output should only include the participants' names and corresponding comments. "
    "The comments must be presented in **Chinese Simplified**."
    "When a participant's name is included, prepend an '@' right before it, and leave spaces before the '@' and after the name (e.g. 'name' -> ' @name ')."
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix = "!", intents = intents)

active_sessions = {}
channel_configs = {}


# call ai
async def call_ai(messages, model):
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"**🔴 Error**:\nFailed communicating with AI.\n{e}"


# background task
async def summary_loop(channel_id):
    while True:
        session = active_sessions.get(channel_id)
        if not session:
            break

        try:
            await asyncio.sleep(session["frequency"] * 60)
        except asyncio.CancelledError:
            break

        messages = session["buffer"]
        session["buffer"] = []

        if not messages:
            continue

        current_text_chunk = "\n".join(messages)

        session["transcript_log"].append(current_text_chunk)

        api_messages = [
            {"role": "system", "content": session["prompt"]},
            {"role": "user", "content": f"Transcript Chunk:\n{current_text_chunk}"}
        ]

        summary = await call_ai(api_messages, MODEL_NAME)

        if len(summary) > 2000:
            summary = summary[:1995] + "..."
        
        await session["output_channel"].send(f"**⏱️ Interval Summary**:\n{summary}")


# bot events
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"**🟢 Bot Online**:\nLogged in as {bot.user}.")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    channel_id = message.channel.id

    if channel_id in active_sessions:
        active_sessions[channel_id]["buffer"].append(message.content)


# slash commands
@bot.tree.command(name="config", description="Configure settings")
async def config(interaction: discord.Interaction, minutes: int, prompt: str = None):
    channel_id = interaction.channel_id
    
    if channel_id not in channel_configs:
        channel_configs[channel_id] = {}
    
    channel_configs[channel_id]["frequency"] = minutes
    if prompt:
        channel_configs[channel_id]["prompt"] = prompt
    else:
        if "prompt" not in channel_configs[channel_id]:
            channel_configs[channel_id]["prompt"] = DEFAULT_PROMPT

    if channel_id in active_sessions:
        session = active_sessions[channel_id]
        session["frequency"] = minutes
        if prompt:
            session["prompt"] = prompt

        session["task"].cancel()
        session["task"] = bot.loop.create_task(summary_loop(channel_id))
        await interaction.response.send_message(f"**🔵 Configuration Updated**:\nFrequency: {minutes} minutes")
    else:
        await interaction.response.send_message(f"**🔵 Configuration Saved**:\nFrequency: {minutes} minutes")


@bot.tree.command(name="start", description="Start monitoring")
async def start(interaction: discord.Interaction, output_channel: discord.TextChannel):
    channel_id = interaction.channel_id

    if channel_id in active_sessions:
        await interaction.response.send_message("**🟡 Invalid Action**:\nAlready monitoring!", ephemeral=True)
        return
    
    config_data = channel_configs.get(channel_id, {})
    frequency = config_data.get("frequency", 2)
    prompt_text = config_data.get("prompt", DEFAULT_PROMPT)

    active_sessions[channel_id] = {
        "output_channel": output_channel,
        "frequency": frequency,
        "prompt": prompt_text,
        "buffer": [],
        "transcript_log": [], 
    }

    task = bot.loop.create_task(summary_loop(channel_id))
    active_sessions[channel_id]["task"] = task

    current_date = datetime.now().strftime("%m/%d/%Y")
    await output_channel.send(f"# Summary: {current_date}")

    await interaction.response.send_message(
        f"**🟢 Session Started**:\nMonitoring for messages from other bots/users and generating summaries.\n"
        f"Output to: {output_channel.mention}\n"
        f"Frequency: Every {frequency} minutes"
    )


@bot.tree.command(name="stop", description="Stop and evaluate")
async def stop(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    
    if channel_id not in active_sessions:
        await interaction.response.send_message("**🔴 Error**:\nNo active session.", ephemeral=True)
        return

    session = active_sessions[channel_id]
    session["task"].cancel()

    await interaction.response.send_message(f"**🟠 Session ended**:\nYou can check the summaries and participant evaluations at {session["output_channel"].mention}.\n")

    if session["buffer"]:
        last_chunk = "\n".join(session["buffer"])
        session["transcript_log"].append(last_chunk)
        
        api_messages = [
            {"role": "system", "content": session["prompt"]},
            {"role": "user", "content": f"Transcript Chunk:\n{last_chunk}"}
        ]
        last_summary = await call_ai(api_messages, MODEL_NAME)
        await session["output_channel"].send(f"**⏱️ Interval Summary**:\n{last_summary}")

    full_meeting_text = "\n\n".join(session["transcript_log"])
    
    final_messages = [
        {"role": "system", "content": "You are a meeting evaluator."},
        {"role": "user", "content": f"Here is the complete transcript of the meeting:\n{full_meeting_text}"},
        {"role": "user", "content": FINAL_EVAL_PROMPT}
    ]
    
    final_evaluation = await call_ai(final_messages, MODEL_NAME)
    
    if len(final_evaluation) > 2000:
        final_evaluation = final_evaluation[:1995] + "..."
        
    await session["output_channel"].send(f"**📊 Participant Evaluation**:\n{final_evaluation}")

    # Cleanup
    del active_sessions[channel_id]


bot.run(DISCORD_TOKEN)
