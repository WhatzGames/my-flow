---
name: build-android
description: Build Android through the isolated My Flow copy-on-write workflow while requiring AOSP and kernel adjustments to be stored as repository .patch files.
---

# Build Android

Use the containerized builder worktree for every Android build. Do not run `repo`, `make`, `ninja`, a device packaging script, or an ad-hoc Docker/Podman build container directly.

Never edit the AOSP checkout or kernel source directly. Create every AOSP or kernel adjustment as a `.patch` file in the Device Tree or container-builder `patches/` directory. The approved `container-build.sh` may apply those patches to the private COW overlays; it must not contain inline `sed`, `perl`, Python, copy, redirect, or container commands that alter AOSP/kernel source.

## Pinned setup

- AOSP manifest: `android-4.4.4_r2.0.1`
- AOSP base volume: `aosp-android-4.4.4-r2.0.1-base`
- Builder image: `localhost/aosp-kitkat-wheezy:cow`
- Shared compiler cache: `aosp-ccache`
- AOSP and kernel sources: private Podman `:O` overlays
- Inputs: read-only Device Tree, workspace, repository `.patch` files, and public ADB key
- Private ADB key: never read or mount
- Containers: always explicitly named and never started with `--rm`

## Commands

From the `android_containerized_build-*` worktree, prepare the base once with a meaningful unique container name:

```sh
./prepare-aosp-base.sh aosp-android-4.4.4-r2.0.1-prepare-1
```

Build a device through its `build.env` and `scripts/container-build.sh` contract:

```sh
./build-device.sh /absolute/path/to/android_device_<vendor>_<device>-<branch> <meaningful-container-name>
```

The hook permits the exact builder-image command when the image itself must be rebuilt:

```sh
podman build -t localhost/aosp-kitkat-wheezy:cow .
```

Read-only inspection such as `podman ps`, `podman logs`, and `podman inspect` remains allowed.

Announce a build before starting it. After it starts, do not poll until the user asks for status or says it finished or failed.

The plugin records the named container at `PreToolUse`. When the agent would otherwise stop, its `Stop` hook waits on Podman's completion event. The same agent is automatically continued when the container exits. A successful build triggers artifact verification; a failed build supplies the container exit code, the last log lines, and the saved failure-log path. Do not add a polling loop or delegate monitoring to another task.
