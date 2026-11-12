---
name: sfdx-graphify
description: Build Salesforce DX snapshots, query dependencies, and open the local graph or a documented process map. Use for Apex, Flow, metadata, and specification evidence.
---

# Salesforce dependency evidence

Use the installed `sfdx-graphify` CLI for Salesforce. Leave the generic `graphify` and `graphifyy` commands out of this workflow. Use the executable path the user gave you or the one that worked in this conversation. Run `sfdx-graphify --version` if the installation is uncertain and `sfdx-graphify guide` when you need more command details. Report a missing CLI as a prerequisite. Ask the user for permission before installing, updating an installation, or deleting files.

Use the Salesforce project folder containing `sfdx-project.json` as the working directory when building or using the project's default snapshot. Reuse the snapshot the user supplied or the one already selected in this conversation by passing `--snapshot "<path>"`. Otherwise use the project's saved snapshot with versions that support it. Version 0.5.5 requires the explicit snapshot path for queries and the viewer.

For work that needs multiple dependency queries, start or reuse one query server for the selected snapshot before querying. Run `sfdx-graphify query-server --port 0` in a background terminal task. Add `--snapshot "<path>" --expect-snapshot "<snapshot-id>"` when the snapshot was explicitly selected. For a process catalog snapshot, get its path and ID with `sfdx-graphify processes status`. Wait for `Local Salesforce query service:` and use the URL it prints with `--server "<url>" --expect-snapshot "<snapshot-id>"` on each query. This loads and verifies the snapshot once. Check `status --server "<url>"` before reusing a server whose snapshot ID is uncertain. If a background task is unavailable, use a single `session` with JSONL requests or issue a direct query. Stop only a query server started for this task when the task ends.

- **Answer a dependency question.** Run the relevant query against the saved snapshot. Use `--format brief` on 0.5.10 or newer for conversational answers. Use `--format text` for detailed evidence and JSON for structured automation. Use `refs` for incoming references, `deps` for outgoing dependencies, `impact` for incoming paths, `components` to disambiguate a name, and `explain` for the evidence behind a relationship ID. Pass `--expect-snapshot "<snapshot-id>"` on related queries to pin the generation. Check source freshness explicitly when the task requires it.
- **Open the whole graph.** Run `sfdx-graphify serve --port 0` as a background task, adding the same `--snapshot` argument when using an explicit path. Wait for the command to print `Local Salesforce viewer:` and open the printed URL in the local browser. Its root shows the complete map. If browser access is unavailable, give the user that URL and keep the viewer running. Reuse a known running viewer for the same snapshot.
- **Build or update a snapshot.** Run `sfdx-graphify build .` when the user requests a build. If a query needs a snapshot that has yet to be built, ask whether to build it. Record the path from the build's output and reuse it in the same conversation. Report the CLI error if the build fails. Use Git only to inspect the source revision and state. Leave the source files and Git index untouched. Build from the requested revision and record any local changes. Use `--include-untracked` or `--include-inactive` only when those inputs belong in the requested analysis.

For example, `sfdx-graphify refs Account.Description --format text` shows incoming references. To check whether a snapshot matches a working copy, run `sfdx-graphify status --source . --require-current` from that copy, adding `--snapshot` when using an explicit path.

On 0.5.20 or newer, `refs DemoController --format brief` includes references to directly owned methods and reports their counts. For snapshots built by 0.5.20 or newer, use `refs Order.Status --value 'Ready' --format brief` for exact literal write occurrences. Use `refs Order --relation triggered_by --limit 20 --format brief` for declared save automation and `refs Sample_Event__e --format brief` for local publisher and subscriber evidence. A direction inferred from local coverage requires confirmation against the integration or org configuration.

## Process maps

On 0.5.16 or newer, use the registered catalog from the folder containing `sfdx-project.json` when the user asks about a documented business process, a country feature, or a step in the local viewer. Discover the catalog through the CLI. If the project has no registration, request the snapshot and catalog paths for one-time local setup with `sfdx-graphify processes register --snapshot "<path>" --catalog "<path>"`. Report an invalid or missing registered file and ask for its replacement path if needed.

1. Find the process with `sfdx-graphify processes list`, optionally passing `--country ES`, `--feature "<id-or-name>"`, or `--match "<title fragment>"`. Use an ID or the catalog's title language for name matching. If a translated fragment yields no match, list the processes for that country and match the user's intent to their titles and summaries. Ask which process the user means only when several candidates remain plausible.
2. Run `sfdx-graphify processes show "<process-id>"` with the relevant country and feature. This returns the process, its steps, and `viewer_path`. For a question about a specific step, add `--step "<step-id-or-label>"` for effects, components, and source evidence.
3. When the user asks to open a map, join the returned `viewer_url` and `viewer_path` if a matching viewer is running. Otherwise start `sfdx-graphify serve --registered-process-maps` in the background from the same project. Wait for `Local Salesforce viewer:` and append `viewer_path` to the printed URL. Open that link or give it to the user and keep the viewer running. Use the registered snapshot for these actions.

On 0.5.17 or newer, use `processes list --component "<id-or-api-name>"` to find
the process steps that use a component. Use `processes show "<process-id>" --format brief`
for a compact outline with connections, effects, and one source location per effect.
Use `processes status` to inspect the registered revision and files. When comparing
with the current checkout, run `processes check --format brief` and report any references that
need review. The check can report a relocated line without updating the map.
Use `refs "<component>" --registered` for dependencies on the snapshot pinned by
the catalog. Use `processes country "<process-id>" --country <code> --country <code>`
for the declared country decisions and state the remaining conditions.

For a process answer or OpenSpec review, state the selected process and step, the condition that controls the branch, and the operation on the relevant object or field. Distinguish assignments in memory, attempts to persist records, and calls to another component. Cite the returned `path` and `line`. Use `snapshot_matches` for relationships backed by the graph and identify other citations as documented source evidence. Treat both as static evidence at the returned `source_revision` and `snapshot_id`. Edit OpenSpec files only when the user requests edits.

## Evidence and coverage

On 0.5.15 or newer, permission extraction is opt-in with `sfdx-graphify build . --include-permissions`. If the snapshot includes permissions, use `sfdx-graphify refs Account.Description --relation grants --format brief` to find explicit Profile and PermissionSet declarations. Report the operation and XML location. Effective access depends on assignments, sharing, and org configuration. Use `sfdx-graphify view Account.Description --mode permissions` for a focused graph. To trace a Named Credential, run `refs sf:namedcredential:<api-name>` and inspect the Apex source evidence.

Inspect coverage, relevant diagnostics, truncation and candidate calls. Use the literal mention paths in field and Apex method refs to guide source review. Add `--full-coverage` when the compact text summary leaves a coverage question open. Verify consequential edges against the cited Apex or XML. Use source file and line or XML locator in citations, with the relationship ID when it helps reproduce the query. A missing edge leaves the dependency unknown. Local metadata provides static evidence and leaves deployed org behavior unverified.

For a specification snapshot, use a clean checkout of the requested branch with the default tracked-file scope. Verify and record the branch and full Git commit before and after the build. Retain the snapshot ID, extractor and schema versions, dirty status, selection and activity scope, and freshness. Read these from the active generation manifest and the explicit source check. Keep the resulting claims scoped to that evidence and distinguish verified relationships from candidates. Preserve snapshots and source locally, including logs and paths that identify private projects.
