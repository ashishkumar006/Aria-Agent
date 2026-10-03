The VisionFile skill understands a LOCAL image or screenshot the user
references. It sends the image (path in `metadata.path`) to the V9
multimodal vision model and returns its description or answer to the
`metadata.goal` (or a general description if no goal is given).

This is NOT a web skill — there is no fetch, no browser cascade. The image
is already on disk; the skill just reads it and asks the vision model about
it.

Inputs: `metadata.path` (required, a local image file — png/jpg/jpeg/gif/
bmp/webp), `metadata.goal` (optional free-text question about the image).
Output: a `content` string with the model's description / answer.

Use for: "what's in this screenshot?", "read the text in this image",
"describe my photo", "what error is shown in this picture?", "extract the
table from this scan", etc.

If the path is missing, not a file, or not a supported image type, the
skill returns an error and the orchestrator surfaces it — do not retry
with a guessed path.
