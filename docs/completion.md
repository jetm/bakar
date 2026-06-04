# Shell completion

bakar supports tab-completion for subcommands, flags, and preset names. Setup
differs by shell.

## bash / zsh

Run once to install the completion script:

```bash
bakar --install-completion
```

Reload your shell or open a new terminal. All subcommands and flags complete
automatically, and `--preset` completes from your defined presets.

## fish

Typer's built-in completion does not support fish. Use the generator script
that ships with the source:

```bash
cd ~/path/to/bakar  # or wherever you cloned/installed from
uv run scripts/gen-fish-completion.py > ~/.config/fish/completions/bakar.fish
```

Reload in the current session:

```fish
source ~/.config/fish/completions/bakar.fish
```

The generated file covers all subcommands and flags. `--preset` completes
dynamically from your defined presets by calling `bakar presets list`.

### Keeping the file up to date

Regenerate after any bakar update that adds new subcommands:

```bash
uv run scripts/gen-fish-completion.py > ~/.config/fish/completions/bakar.fish
```

If you manage dotfiles with chezmoi, track the file:

```bash
# First time
chezmoi add ~/.config/fish/completions/bakar.fish

# After regenerating
cp ~/.config/fish/completions/bakar.fish \
   ~/.local/share/chezmoi/private_dot_config/fish/completions/bakar.fish
```

## What completes

| Context | Completions |
|---------|-------------|
| `bakar <TAB>` | All subcommands |
| `bakar build --<TAB>` | All `build` flags |
| `bakar build --preset <TAB>` | Preset names from `config.toml` and `vendors.toml` |
| `bakar presets <TAB>` | `list show add remove` |
| `bakar settings <TAB>` | `get set list unset` |
| `bakar layers <TAB>` | `inspect status` |

Preset completion reads from config without triggering a sync.
