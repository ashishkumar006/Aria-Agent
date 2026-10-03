// Project rule, enforced structurally: no session may read secret files.
// Blocks the `read` tool for `.env` (any suffix variant such as
// `.env.local`) while still allowing the template `.env.example`.
// Writes are intentionally NOT blocked: key rotations happen rarely and
// only on explicit user request.
export const EnvProtection = async () => {
  const isSecretEnv = (p) => {
    const base = String(p || "").split(/[\\/]/).pop() || "";
    if (base === ".env") return true;
    if (/^\.env\./i.test(base) && base.toLowerCase() !== ".env.example") return true;
    return false;
  };
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool === "read" && isSecretEnv(output.args && output.args.filePath)) {
        throw new Error(
          "Do not read .env files (project rule — ask the user for the value instead)"
        );
      }
    },
  };
};
