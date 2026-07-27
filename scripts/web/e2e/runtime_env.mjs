const CHILD_ENV_ALLOWLIST = new Set([
  "APPDATA",
  "COMSPEC",
  "HOME",
  "HOMEDRIVE",
  "HOMEPATH",
  "LANG",
  "LOCALAPPDATA",
  "LOGNAME",
  "PATH",
  "PATHEXT",
  "PWD",
  "REQUESTS_CA_BUNDLE",
  "SHELL",
  "SSL_CERT_FILE",
  "SYSTEMROOT",
  "TEMP",
  "TMP",
  "TMPDIR",
  "USER",
  "USERPROFILE",
  "UV_CACHE_DIR",
  "UV_LINK_MODE",
  "UV_PROJECT_ENVIRONMENT",
  "UV_PYTHON",
  "VIRTUAL_ENV",
]);
const CHILD_ENV_SECRET_NAMES = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "DASHSCOPE_API_KEY",
  "ALIBABA_CLOUD_ACCESS_KEY_ID",
  "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
]);
const CHILD_ENV_SECRET_PREFIXES = ["ALIYUN_", "AWS_", "GOOGLE_", "AZURE_"];
const CHILD_ENV_SECRET_SUFFIX = /(?:^|_)(?:TOKEN|SECRET|KEY)$/i;

function isSecretEnvName(name) {
  const upperName = name.toUpperCase();
  return (
    CHILD_ENV_SECRET_NAMES.has(upperName) ||
    CHILD_ENV_SECRET_PREFIXES.some((prefix) => upperName.startsWith(prefix)) ||
    CHILD_ENV_SECRET_SUFFIX.test(upperName)
  );
}

export function scrubbedChildEnv({
  configDir,
  homeDir = "",
  repoRoot,
  sourceEnv = process.env,
  platform = process.platform,
}) {
  const env = {};
  for (const [name, value] of Object.entries(sourceEnv)) {
    const upperName = name.toUpperCase();
    if (!CHILD_ENV_ALLOWLIST.has(upperName) && !upperName.startsWith("LC_")) {
      continue;
    }
    if (isSecretEnvName(name) || value === undefined) {
      continue;
    }
    env[name] = value;
  }
  if (homeDir) {
    env.HOME = homeDir;
  }
  if (homeDir && platform === "win32") {
    const windowsHome = homeDir.replace(/[\\/]+$/, "");
    env.USERPROFILE = windowsHome;
    env.APPDATA = `${windowsHome}\\AppData\\Roaming`;
    env.LOCALAPPDATA = `${windowsHome}\\AppData\\Local`;
  }
  if (homeDir && platform !== "win32") {
    env.SHELL = "/bin/zsh";
  }
  env.IAC_CODE_CONFIG_DIR = configDir;
  env.IAC_CODE_CWD = repoRoot;
  return env;
}
