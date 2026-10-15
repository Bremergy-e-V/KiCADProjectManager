# KiCad Project Manager

A desktop project browser for teams that keep their KiCad projects in git, made for people who don't know git and don't want to. It works with **Gitea / Forgejo**, **GitHub** and **GitLab**.

It works much like the project view in Altium 365. You see every project of the team in a shared folder structure, with a preview image and when it was last changed. Double-click a project to download it (or get the latest version) and open it in KiCad. When you're done, click **Save to server**.

Git runs underneath, but nobody has to set it up or type a git command.

## Features

- **Shared folder structure.** Create folders and nest them, then drag & drop projects and folders to reorganize. Everyone sees the same structure.
- **Project cards** with a preview image (a 3D render of the board, or the schematic early in a project), when the project was last changed and by whom, and a status:
  - *Up to date*
  - *2 new changes on server*
  - *Changes not on server yet*
  - *Not downloaded*
- **Double-click to work.** The first time, this downloads the project. After that, it gets the latest version and opens the project in KiCad.
- **New Project** creates the repository on the server, sets up a KiCad project from one of your team templates (or a blank one) with sensible `.gitignore`/`.gitattributes` files, and adds it to the current folder.
- **Add Existing** adds any number of existing repositories to the current folder.
- **Import Local** uploads a KiCad project from your computer: it creates a new repository, copies the project into your projects folder (without backups, lock and autosave files) and adds it to the current folder. The original folder is left untouched.
- **Convert to Template** (right-click a project) turns the server version of a project into a template that everyone can pick under *New Project*.
- **Revert to Server State** (right-click a project) throws away all your local changes, including saves that never reached the server, after asking for confirmation.
- **Save to server** uploads your changes with a short description. If a teammate changed something else in the meantime, the two sets of changes are combined automatically. If you both changed the same thing, nothing is lost: your version goes to a separate branch on the server and you get a clear message (see [Conflicts](#conflicts)).
- **No git setup needed.** You sign in with a personal access token. The app passes it to git itself, so there are no SSH keys or credential helpers to configure, and the token is never written into a remote URL. The token is kept in the system keychain, and commits are made with your account's name.
- **Updates itself.** On start, the app checks for a new release and offers to install it. You can choose *Update Now*, *Not Now* or *Skip This Version*.

## Installation

Download the newest build from the [releases page](https://github.com/Bremergy-e-V/KiCADProjectManager/releases/tag/latest).

| Platform | Download | |
|---|---|---|
| Windows 10/11 | `KiCadProjectManager-windows-x64-setup.exe` | Installs for the current user, no admin rights needed. |
| Windows (portable) | `KiCadProjectManager-windows-x64.zip` | Unzip and run `KiCadProjectManager.exe`. Does not update itself. |
| Arch Linux | `kicad-project-manager-*.pkg.tar.zst` | `sudo pacman -U kicad-project-manager-*.pkg.tar.zst` |

You also need:

- [Git](https://git-scm.com/downloads)
- [KiCad](https://www.kicad.org/download/) 8 or newer, including `kicad-cli` for previews

After installing, search for **KiCad Project Manager** in the start menu or your application launcher.

## First start

1. **Sign in.** Choose your server type, enter the server address and paste a personal access token. The dialog links to the right settings page:

   | Server | Token |
   |---|---|
   | Gitea / Forgejo | *Settings → Applications*. Give it *Read and Write* access to **repository** and **organization**, and *Read* access to **user**. |
   | GitHub | *Settings → Developer settings → Personal access tokens*. A classic token needs the **repo** and **read:org** scopes. |
   | GitLab | *Preferences → Access tokens*, with the **api** scope. |

2. **Choose the config repository.** Pick your team's existing config repository. To start fresh, pick an empty repository or click *Create new repository…*. In that case the app sets up a new project list in it and asks for a name and the default owner (organization or group) for new projects.

## How it works

All shared state lives in a **config repository** on your server:

```
kicad-projects.json      folder structure and project list
previews/<id>.png        one preview image per project
templates/<name>/        optional: KiCad projects offered as templates for "New Project"
README.md
```

Each change (new folder, move, rename, new project) works in four steps:

1. Pull the latest config.
2. Apply the change.
3. Commit.
4. Push.

If someone else pushed in between, the app pulls again and re-applies the change on top of their version. Two people reorganizing at the same time never overwrite each other.

Projects are normal repositories on the same server. They're downloaded to `~/KiCadProjects/<owner>/<repo>` by default (you can change this in Settings).

## Team templates

Templates are the starting points offered under *New Project*. For example, one could hold your title block, design rules, 4-layer board stackup and library tables. The easiest way to make one is to set up a project the way you want it, then right-click it and choose **Convert to Template**. You can also put a complete KiCad project into a `templates/<name>/` folder of the config repository by hand. A `template/` folder from older versions is still offered as *Team template*.

Files named like the template's `.kicad_pro` are renamed after the new project. Everything else is copied as is.

## Conflicts

KiCad files are text, so changes by different people to different parts of a project usually combine without trouble. When they can't be combined:

- Your version is pushed to a branch named `conflict/<you>-<date>` on the server.
- Your local copy is reset to the newest server version.
- The app tells you the branch name, and someone familiar with git can then merge the two versions.

Nothing is ever silently overwritten.

## Releases and updates

Every push to `main` builds the Windows installer, the portable zip and the Arch package. It then republishes them as the rolling [`latest`](https://github.com/Bremergy-e-V/KiCADProjectManager/releases/tag/latest) release (see `.github/workflows/release.yml`).

The release also contains a `version.json` with the build number and the SHA-256 of each download. The app compares the build number against its own, and only installs a download whose checksum matches:

- **Windows:** the installer runs silently and the app restarts.
- **Arch Linux:** the package is installed with `pkexec pacman -U`, which asks for your password.

A copy you run from source has build number 0 and never offers updates. The same goes for the portable zip.

## Development

```bash
uv sync
uv run kicad-project-manager
uv run pytest          # uses local bare repositories and fake servers, no network needed
uvx ruff check src tests
```

Set `KPM_DEBUG=1` for debug logging.

To make a source checkout show up in the application launcher on Linux, run `./packaging/linux/install-user.sh`.

## License

MIT
