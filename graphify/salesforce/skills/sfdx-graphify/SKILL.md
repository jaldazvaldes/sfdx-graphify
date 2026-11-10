---
name: sfdx-graphify
description: Inspect local Salesforce DX dependency evidence for metadata, Apex, and Flows. Use for Salesforce references, impact analysis, or source evidence in specifications.
---

# Salesforce dependency evidence

Use the installed `sfdx-graphify` CLI for local Salesforce metadata. Run `sfdx-graphify guide` for the command contract. If the CLI is missing, report the prerequisite before trying to install anything.

Work from the Salesforce project containing `sfdx-project.json`. Start with the snapshot the user provides. If the task calls for a new build, choose an output directory outside the Salesforce project and tool repository. Build from the requested source revision and record any local changes or nondefault selection flags. Use `--include-untracked` or `--include-inactive` only when those inputs belong in the requested analysis.

```sh
sfdx-graphify build "<project-root>" --out "<snapshot-directory>"
sfdx-graphify status --snapshot "<snapshot-directory>" --source "<project-root>" --require-current
```

Use `components` to locate an ambiguous name, `refs` for incoming references, `deps` for outgoing dependencies, `impact` for bounded incoming paths, and `explain` for the evidence behind a relationship ID. Pass `--snapshot "<snapshot-directory>" --expect-snapshot "<snapshot-id>"` on related queries to pin the generation.

Inspect coverage, relevant diagnostics, truncation and candidate calls. Verify consequential edges against the cited Apex or XML. Use source file and line or XML locator in citations, with the relationship ID when it helps reproduce the query. A missing edge leaves the dependency unknown. Local metadata provides static evidence and leaves deployed org behavior unverified.

For a specification snapshot, use a clean checkout of the requested branch with the default tracked-file scope. Verify and record the branch and full Git commit before and after the build. Retain the snapshot ID, extractor and schema versions, dirty status, selection and activity scope, and freshness. Read these from the active generation manifest and the explicit source check. Keep the resulting claims scoped to that evidence and distinguish verified relationships from candidates. Preserve snapshots and source locally, including logs and paths that identify private projects.
