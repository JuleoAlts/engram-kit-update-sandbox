# engram-kit-update-sandbox

Engram memory of the `kit_update_sandbox` project, exported with Engram Git Sync: design and specification documents, decisions, bug fixes, discoveries and session summaries. The user prompts are NOT included.

This repository holds two things: the exported memory (`.engram/`) and the tools that share it (`tools/`, `tests/`).

<!-- engram-memory:project-text:start standard=1959acf61f440fc1 the text of this project goes between this line and the end line; init-memory keeps it byte for byte -->
<!-- engram-memory:project-text:end -->

## Use it

1. Install Engram (3.4 or newer), `git`, `bash` and Python 3.9 or newer (`python3`, `python` or `py -3`: each is run, so the Microsoft Store shortcut that Windows calls `python3` is skipped).
2. Clone this repository and open a terminal inside it.
3. Run `bash tools/engram-memory-sync.sh down`. It pulls this repository, sets the machine up and imports the chunks you do not have yet. It is safe to repeat.
4. To get later updates, run `down` again. To share what you added, run `bash tools/engram-memory-sync.sh up`.

The `kit_update_sandbox` project calls these two commands from its git hooks, so on a configured machine nobody runs them by hand.

## The tools

| File | What it does |
|---|---|
| `tools/engram-memory-sync.sh` | The entry point. `down` and `up`, described below. |
| `tools/sanitize_chunks.py` | Cleans NEW chunks before they are committed: removes prompts and personal data, drops relations that point outside the chunk and blanks their free text (see "Privacy"). It validates every chunk first and only then writes, so a refusal leaves everything untouched. Chunks that git already has are never rewritten. |
| `tools/merge_manifest.py` | Resolves a conflict of `.engram/manifest.json` between developers by keeping the chunks of both sides. |
| `tools/setup.sh` | Creates the local privacy file (`~/.config/engram-memory-tools/patterns.json`). The sync calls it by itself; run it by hand only to add emails or accounts. |
| `tests/` | The unit tests of the tools: `python3 -m unittest discover -s tests` (Python 3.9 or newer). |

### `down`

1. Pulls this repository (it restarts itself once if the pull replaced the script).
2. Configures the machine: the git identity of this clone, if there is none, and the privacy file.
3. Imports the new chunks into your Engram. The import is idempotent.

### `up`

1. Does the same pull first.
2. Exports what is new in your Engram (`engram sync --project kit_update_sandbox`).
3. Cleans only the new chunks, commits them and pushes. A rejected push is retried once after a pull, merging the manifest by union.

Both commands never fail their caller: every problem is one line on stderr starting with `[engram-memory]`, and the exit code is 0.

### Environment overrides

Variables only OVERRIDE what the tools derived by themselves; nothing needs to be set for a normal machine.

| Variable | Effect |
|---|---|
| `ENGRAM_MEMORY_NAME` | Your full name, to be removed from the shared memory. |
| `ENGRAM_MEMORY_EMAILS` | Your emails, comma separated. |
| `ENGRAM_MEMORY_ACCOUNT` | Your GitHub account. |
| `ENGRAM_MEMORY_OTHER` | Other accounts or identities that must never appear, comma separated. |
| `ENGRAM_MEMORY_DRY_RUN=1` | `up` stops before the commit: nothing is committed or pushed. |
| `ENGRAM_MEMORY_TIMEOUT` | Seconds each network step (git pull/push, `engram sync`) may take before it is skipped. Default 90, `0` for no limit. Uses `timeout` or `gtimeout` when installed. |
| `ENGRAM_DATA_DIR` | An Engram database other than `~/.engram` (use an absolute path; it is how the tests stay isolated). |

A bot identity (`Claude`, `noreply@anthropic.com`) or a system user (`root`, `runner`) names no one and is never used as a person.

## Privacy

Before a chunk is committed, the cleaning step removes prompts, the owner's name, emails, GitHub account, computer username and home folder (as whole words and whole paths only), and masks any other email address. The privacy file stays on the computer (mode 600) and is never committed. Write observations as project knowledge: no emails, accounts, tokens or personal working style.

## Rules

- Never commit a chunk by hand, never edit `.engram/manifest.json` and never run a raw `engram sync` (export) inside this folder: the tools do it, in order.
- Never rewrite a chunk that is already published: a chunk id is the first 8 characters of the SHA-256 of its content, and a changed chunk is imported by every machine as new data that overwrites whole observations.
- Never delete or share `~/.engram/local-only/`: the raw originals there are the only way back after an import.
- Never rewrite the history of this repository.
- The tests use their own temporary folders and a fake `engram`; never point a test at your real Engram database.

## The weekly update from the kit

Nobody has to remember the kit. `.github/workflows/engram-kit-update.yml` reads engram-memory-kit at its default branch every Monday (or when started by hand), runs that kit's own init command on this repository with `--force`, and looks at the result. It runs on GitHub, so it changes nothing on a developer's machine until somebody merges what it proposes.

| What it finds | What it does |
|---|---|
| Nothing changed | Nothing |
| Something changed | One pull request on the branch `engram-kit-update/from-kit`, updated on later weeks instead of opening another. Its text names the commit of the kit and what a reviewer looks at. It never merges and never pushes to the default branch |
| Only the workflow file itself changed | One issue with the command: the token of a workflow may not change a workflow file, so a person commits that one file. When other files changed too, the pull request says it instead |
| Actions may not open pull requests here | One issue saying what to enable |
| The kit proposes a file outside the list held in the workflow, a symlink or a deletion | One issue, and nothing is pushed. A new standard file needs a person to update the workflow, and the issue gives the command |
| The kit cannot be read | One issue naming the secret `ENGRAM_KIT_SSH_KEY` and the steps below |
| The kit does not run here | One issue with what it said |
| The kit refused to change a file (see "the markers near the top of this file") | One issue naming the file and how to fix it, next to the pull request when there is one, plus a warning in the log. Close it to say you have seen it: no new one opens until the refusal says something different |

An issue is not repeated while it is open. Close it after you fix what it says; if the problem is still there, the next run opens a new one.

**Its pull request may need a person to start the checks.** GitHub holds the checks of a pull request opened by the token of a workflow: a banner in the merge box lets a person with write access start them with **Approve workflows to run**. If there is no banner and no checks, GitHub started none, which is how it used to behave, and closing and reopening the pull request starts them. A broader token would avoid that, and this workflow does not ask for one.

**The format of the generated tests.** They are formatted with the prettier options of the kit (`semi`, `singleQuote`, `printWidth: 100`, `trailingComma: es5`, `arrowParens: always`, `endOfLine: lf`), and the workflow runs no formatter of this repository: it installs nothing, and a formatter it cannot try is not one it can promise. No git hook runs on its commit either. If the formatter or the lint rules of this repository differ, a check of the format can fail on the pull request, and a file reformatted here shows up again as a difference: add that folder to the ignore lists of the formatter and the linter.

**A commit made by hand on its branch** stays there while the kit proposes the same change, also when the default branch moves. When the kit proposes something new, the branch is replaced, what it held is saved on a branch named `engram-kit-update/replaced-<commit>`, and a comment on the pull request says so. Something meant to last goes on the default branch.

Whatever is this repository's own is never overwritten, not even with `--force`: write it between the two markers near the top of this file, and an edit outside them or a file with no markers is refused. What the kit refused is listed in that issue and in the pull request.

A person with admin rights does this once per repository. It is what makes the workflow work; nothing else needs doing later.

1. **Let it open pull requests.** In this repository: Settings, Actions, General, Workflow permissions, and tick "Allow GitHub Actions to create and approve pull requests". If it is greyed out, the same setting of the organization comes first.
2. **Let it read the kit,** while the kit is private. A deploy key can only read that one repository, and only the workflow of this repository holds it. With no secret the workflow tries without one, which is enough once the kit is public.

   ```sh
   ssh-keygen -t ed25519 -N "" -C "engram-kit-update" -f engram-kit-key
   gh repo deploy-key add engram-kit-key.pub --repo juankmilo65/engram-memory-kit --title "engram-kit-update engram-kit-update-sandbox"
   gh secret set ENGRAM_KIT_SSH_KEY --repo juankmilo65/engram-kit-update-sandbox < engram-kit-key
   rm engram-kit-key engram-kit-key.pub
   ```

   Adding the deploy key needs admin rights on the kit. In the browser: the kit's Settings, Deploy keys, with "Allow write access" off; this repository's Settings, Secrets and variables, Actions, a new repository secret named `ENGRAM_KIT_SSH_KEY`, holding the private key.
3. **Try it:** Actions, engram-kit-update, Run workflow.

## How it is made

- Each export is a new compressed chunk in `.engram/chunks/`; existing chunks are never edited.
- The chunk is produced by `engram sync --project kit_update_sandbox` and its prompts are removed before it is committed.
- `.engram/config.json` names the project (`kit_update_sandbox`); the tools read the project name from it.
