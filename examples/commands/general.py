"""Example module. Copy this file to add your own commands.

Load it with ``bot.modules.load("cmd/general.py")``, unload with
``bot.modules.unload("general")``, reload with
``bot.modules.reload("general")``.
"""

from despise.cmd import Module, command, listen


class General(Module):
    name = "general"

    @command("ping", aliases=["p"])
    async def ping(self, ctx):
        await ctx.reply("pong")

    @command("echo")
    async def echo(self, ctx):
        await ctx.reply(ctx.text or "(nothing to echo)")

    @listen("message")
    async def log(self, msg):
        print(f"#{msg.channel_id} {msg.author.username}: {msg.content}")
