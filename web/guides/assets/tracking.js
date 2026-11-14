(() => {
  const params = new URLSearchParams(window.location.search);
  const download = document.querySelector("[data-download]");
  if (!download) return;
  const url = new URL(download.getAttribute("href"), window.location.origin);
  for (const [key, value] of params) {
    if (key.startsWith("utm_") && value) url.searchParams.set(key, value);
  }
  download.href = url.pathname + url.search;
})();
