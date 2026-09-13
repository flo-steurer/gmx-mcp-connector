# Test Plan — awita-mail MCP server

## Scope

- Verify authenticated Streamable HTTP access to the GMX mailbox, read-only tools, draft lifecycle, and safety gates.
- Keep `ALLOW_SEND=false` for the main run; enable sending only for the optional final check.

## Prerequisites

- Start the server with the working `.env` and keep its terminal visible:
  `python -m app`
- Confirm `GET http://127.0.0.1:8000/health` returns `200` with only `status`, `service`, and `version`.
- Use a local MCP Inspector connected to `http://127.0.0.1:8000/mcp` with:
  `Authorization: Bearer <exact MCP_API_TOKEN>`.
- Have a test address you control, at least one inbox message, and preferably one unread message and one message with an attachment.
- Record the exact `folder`, `uid`, and `uid_validity` returned for any message used in later calls.

## Happy Path — read-only mailbox

1. Run `list_folders` with `{}`. Expect discovered GMX names, IMAP flags, and resolved roles. Do not assume English folder names.
2. Run `check_gmx_connectivity` with `{}`. Expect `imap: "ok"`, `smtp: "ok"`, and a resolved-folder map. Confirm no message is sent.
3. Run `list_emails` with:
   `{ "folder": "inbox", "limit": 5, "unread_only": false }`.
   Expect metadata only, bounded results, a complete message reference, and `trust: "untrusted_external_data"`.
4. Run `search_emails` with a known subject or sender and a small limit. Add `after`/`before` dates in `YYYY-MM-DD` form and verify the result remains bounded.
5. Run `read_email` using one exact returned message reference. Expect decoded Unicode, readable plain text (including HTML-only mail converted to text), attachment metadata, and an explicit truncation flag when applicable.
6. If the message has attachments, run `list_attachments`. Expect filename, MIME type, decoded size, disposition, and MIME part ID, but no content.
7. Run `download_attachment` with one returned attachment ID. Expect an MCP embedded blob resource. Verify that no file is written, extracted, or executed.
8. Run `mark_as_read`, then `mark_as_read` again; run `mark_as_unread`, then `mark_as_unread` again. Expect idempotent success and no other flag changes.

## Happy Path — draft lifecycle (`ALLOW_SEND=false`)

1. Run `create_draft` addressed only to your test address:

   ```json
   {
     "to": ["YOUR_TEST_ADDRESS"],
     "subject": "awita-mail draft test",
     "body": "Draft-only test; do not send automatically."
   }
   ```

   Expect a draft reference and Message-ID. Verify the draft appears in the discovered drafts folder in GMX.
2. Run `update_draft` with the returned draft reference and a changed subject/body. Expect a new message reference, one replacement draft, preserved threading headers, and no duplicate old draft.
3. Run `create_reply_draft` against an inbox message with a short body. Verify the reply targets the sender or `Reply-To`, excludes the awita address, uses one `Re:` prefix, and preserves `In-Reply-To`/`References`.
4. Run `delete_draft` against the test draft. Expect success and confirm only that draft disappeared; unrelated deleted messages must remain untouched.
5. Run `send_draft` against a verified draft while `ALLOW_SEND=false`. Expect a generic rejection and confirm GMX received no message.

## Edge Cases

1. Call `list_emails` with `limit: 0` and `limit: 101`. Expect validation errors or clamping only within the documented safe range; never more than 100 results.
2. Call `create_draft` with an invalid address, more than 20 recipients, a subject over 255 characters, or a body over the configured limit. Expect rejection and no draft created.
3. Call `search_emails` with CR/LF or control characters in `text`, `sender`, `recipient`, or `subject`. Expect rejection and no IMAP search.
4. Call `read_email`, `update_draft`, or `delete_draft` with an altered `uid_validity`. Expect stale-reference rejection and no mailbox mutation.
5. Call `download_attachment` with an invalid MIME part ID or an attachment above the configured decoded-size limit. Expect rejection and no local file output.
6. Re-read an unread message after `read_email`. Verify it remains unread.

## Negative / Error Paths

1. Request `/mcp` without a bearer token and with a wrong token. Expect `401`; do not expose secrets in the response or logs.
2. Request `/health` without authentication. Expect `200` and no mailbox details.
3. Call draft-only mutation tools with an inbox or sent-message reference. Expect rejection because the reference is not a verified draft.
4. Use a valid-looking MIME part from another message. Expect rejection because the reference or part cannot be validated for that message.
5. Include tool-like instructions inside a test email. Verify they are returned as untrusted data and never cause another tool call or send.
6. Inspect Inspector request traces and server logs. Confirm `Authorization`, credentials, full bodies, and attachment bytes are not logged.

## Optional send check (explicitly approved, temporary)

1. Stop the server, set `ALLOW_SEND=true`, restart it, and create a new draft addressed only to your own test address.
2. Review that draft in GMX, then call `send_draft` once. Expect `sent` and a Sent-folder copy matched by Message-ID; the draft should be retired.
3. Repeat the exact `send_draft` call. Expect `already_sent` or an equivalent replay block and no duplicate delivery.
4. Set `ALLOW_SEND=false` again and restart the server.

## Regression Risks

1. Restart the service and verify `.env` loading, local audit database creation, and MCP reconnection still work.
2. Verify the Inspector lists all intended tools and no arbitrary send, bulk delete, forwarding, rule, or move tool.
3. Confirm read-only tools do not mark messages seen and write annotations remain accurate.
4. If deploying with Docker, verify the normal Compose file publishes no host port, runs as non-root, and persists `/data/audit.db`.

## Out of Scope

- ChatGPT deployment, OAuth/OIDC replacement, Secure MCP Tunnel, and public Traefik exposure.
- High-volume/concurrency testing and deliberate SMTP connection-loss simulation.
