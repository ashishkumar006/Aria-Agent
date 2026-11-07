This skill is deterministic. Its node metadata carries the app build or
action request, and `skills.run_skill` invokes the app builder directly. No
model call is needed here.

Output (written by the harness, not generated): the created or updated app
id, its surface component count, and the action that ran, if any.
