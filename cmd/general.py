"""Example module. Copy this file to add your own commands.

Load it with ``bot.modules.load("cmd/general.py")``, unload with
``bot.modules.unload("general")``, reload with
``bot.modules.reload("general")``.
"""

from despise.cmd import Module, command, listen


class General(Module):
    @command("ping", aliases=["p"])
    async def ping(self, ctx):
        await ctx.reply(f"pong ({ctx.author.username})")

    @command("echo")
    async def echo(self, ctx, *, text: str):
        await ctx.reply(text)

    @command("add")
    async def add(self, ctx, x: int, y: int = 0):
        await ctx.reply(str(x + y))

    @listen("message")
    async def log(self, msg):
        print(msg.author.username, msg.content)
