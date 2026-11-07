---
name: netdisk-operation
version: 1
description: >-
  Use this skill for controlled MoviePilot netdisk operations: list platforms,
  accounts, capabilities, and quota, browse files, and execute explicitly
  confirmed share, move, rename, and delete actions. It authorizes only the
  structured moviepilot_api gateway; it does not authorize login automation,
  arbitrary HTTP, URLs, credentials, Cookies, or tokens.
allowed-tools: moviepilot_api
allowed-api-operations: >-
  netdisk.platforms netdisk.accounts netdisk.account netdisk.files.capabilities
  netdisk.files.quota netdisk.files.list netdisk.files.share netdisk.files.move
  netdisk.files.rename netdisk.files.delete
---

# Netdisk Operation

Use structured `moviepilot_api` operations only; discover their current schemas
instead of guessing fields. Never request, print, or pass a password, Cookie,
token, QR code, SMS code, or arbitrary URL. Login remains in the existing
MoviePilot web interface.

## Read-Only Flow

1. `netdisk.platforms` lists the configured netdisk platforms and their support
   flags.
2. `netdisk.accounts` lists accounts without secrets; `netdisk.account` reads one
   exact account. Reuse the returned identifiers verbatim in later calls.
3. `netdisk.files.capabilities` reports which file operations the target
   platform/account supports; `netdisk.files.quota` reads its quota.
4. `netdisk.files.list` browses one directory. Pass the platform, account, and
   path exactly as returned by prior reads; do not invent or normalize paths.

Read operations execute without confirmation once authorization passes. Report
a platform's explicit "not supported" result instead of retrying with another
operation or raw HTTP.

## Write Flow

These operations change external state and MUST be confirmed by the user before
execution:

- `netdisk.files.share` creates a share. Confirm the exact files/directories,
  expiration, and password behavior first.
- `netdisk.files.move` moves files or directories. Confirm the exact source and
  destination before executing.
- `netdisk.files.rename` renames one item. Confirm the exact current and new
  names before executing.
- `netdisk.files.delete` deletes files or directories. Follow the destructive
  boundary below.

State the exact operation, target identifiers, and expected effect, then wait
for explicit user confirmation. If the user changes the target or scope,
confirm the new request again before executing. Never chain an unconfirmed
follow-up write from a previous confirmation.

## Host Confirmation

The host enforces confirmation; this skill cannot bypass it. Before executing
any of the four writes, the host pauses the call and asks the user, who must
explicitly confirm. Entries without an interactive confirmation handler fail
closed and execute nothing.

- Do not claim that a write has run, or can run, without the host confirmation.
- If a result returns `confirmation_required`, the call was not executed. Ask
  the user to confirm instead of retrying, renaming the operation, or switching
  to another account or raw HTTP.
- A cancelled confirmation executes nothing. After a timeout or an unknown
  result, re-read the target state with the matching read operation before
  retrying, and never promise that the change is recoverable.

## Destructive Delete

`netdisk.files.delete` is destructive. Confirm the exact files and directories,
including whether the target is a directory and what it contains, before
executing. After the call, reconcile the external state by re-reading the
parent directory with `netdisk.files.list` and report the observed result. Do
not promise or imply that deleted content is recoverable. If the reconciled
state is unclear or the call fails, stop and report the blocker instead of
retrying with broader scope.

## Boundaries

- Do not perform login automation: no QR-code, SMS, password, Cookie, token, or
  session bootstrap. Ask the user to complete login in the existing interface.
- Do not issue arbitrary HTTP requests, arbitrary URLs, or custom
  authentication headers; only the declared operations are authorized.
- Results may contain private account data such as nicknames, masked
  credentials, quota, and file listings. Do not echo credentials, Cookies, or
  tokens; report only the structured, masked values returned by the gateway.
- Report stable error codes and the explicit unsupported/denied result as
  returned. Do not fall back to partial writes, other accounts, or unregistered
  operations.
