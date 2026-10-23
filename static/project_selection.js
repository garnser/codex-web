export function projectIdFromPath(pathname=window.location.pathname) {
  const match=String(pathname||"").match(/\/projects\/([^/]+)(?:\/|$)/);
  if(!match)return "";
  try{return decodeURIComponent(match[1])}catch{return match[1]}
}

// The canonical context can be present before the shell populates its selector.
// An explicitly cleared or unavailable context must never fall back to a route.
export function selectedProjectId() {
  if (document.body?.classList.contains("project-context-unavailable")) return "";
  if (document.body && "projectId" in document.body.dataset) {
    return String(document.body.dataset.projectId || "").trim();
  }
  const selector = document.getElementById("product-project-switcher");
  if (selector) return selector.value;
  return projectIdFromPath() || new URLSearchParams(location.search).get("project") || "";
}
