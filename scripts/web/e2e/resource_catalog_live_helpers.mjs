function decodeSafely(value) {
  try {
    return decodeURIComponent(value);
  } catch (_error) {
    return value;
  }
}

export function locationContainsIdentifier(rawUrl, identifier) {
  const matches = (url) => {
    const segments = url.pathname.split("/").filter(Boolean).map(decodeSafely);
    if (segments.includes(identifier)) return true;
    return [...url.searchParams.values()].some((value) => value === identifier);
  };
  try {
    const url = new URL(rawUrl);
    if (matches(url)) return true;
    const hash = url.hash.replace(/^#/, "");
    return hash ? matches(new URL(hash.startsWith("/") ? hash : `/${hash}`, "https://hash.invalid")) : false;
  } catch (_error) {
    return false;
  }
}

export function textContainsIdentifier(text, identifier) {
  const escaped = identifier.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^\\p{L}\\p{N}_.:-])${escaped}(?=$|[^\\p{L}\\p{N}_.:-])`, "u").test(text);
}

export const CONSOLE_NOT_FOUND_ERRORS = [
  /does not exist in our records/i,
  /ResourceNotFound[.A-Za-z0-9_-]*/i,
  /Invalid[A-Za-z0-9_.-]*Id\.NotFound/i,
  /(?:EntityNotExists|OBJECT_NOT_EXIST)[.A-Za-z0-9_-]*/i,
  /(?:资源|实例|集群|监听|安全组|云盘|磁盘|负载均衡|模板|参数|补丁基线).{0,16}(?:不存在|未找到)/,
  /未查询到当前.{0,16}信息.{0,24}(?:返回|跳转).{0,12}列表/,
  /当前指定的.{0,16}不存在/,
];

export const CONSOLE_ACCESS_ERRORS = [
  /(?:无权|没有权限|Forbidden|AccessDenied)/i,
];
