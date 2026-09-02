"""Shell completion generation."""

from __future__ import annotations

import click


@click.group("completion")
def completion_group() -> None:
    """Generate shell completions."""


@completion_group.command("bash")
def completion_bash() -> None:
    """Output bash completion script."""
    click.echo(_get_completion("bash"))


@completion_group.command("zsh")
def completion_zsh() -> None:
    """Output zsh completion script."""
    click.echo(_get_completion("zsh"))


@completion_group.command("fish")
def completion_fish() -> None:
    """Output fish completion script."""
    click.echo(_get_completion("fish"))


def _get_completion(shell: str) -> str:

    env_var = "_TLGR_COMPLETE"
    scripts = {
        "bash": f'eval "$({env_var}=bash_source tlgr)"',
        "zsh": f'eval "$({env_var}=zsh_source tlgr)"',
        "fish": f"{env_var}=fish_source tlgr | source",
    }

    if shell == "bash":
        return f"# Add to ~/.bashrc:\n# {scripts['bash']}\n\n{scripts['bash']}"
    elif shell == "zsh":
        return f"# Add to ~/.zshrc:\n# {scripts['zsh']}\n\n{scripts['zsh']}"
    elif shell == "fish":
        return (
            f"# Add to ~/.config/fish/completions/tlgr.fish:\n"
            f"# {scripts['fish']}\n\n"
            f"{scripts['fish']}"
        )
    return ""
