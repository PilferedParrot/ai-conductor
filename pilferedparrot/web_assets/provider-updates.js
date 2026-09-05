/* A separate request keeps session opening independent of registry availability. */
(() => {
  let selection = "";
  let generation = 0;
  globalThis.PilferedParrotUpdates = {
    check(api, provider, session, label) {
      const key = `${provider}:${session}`;
      if (key === selection) return;
      selection = key;
      const request = ++generation;
      const node = document.querySelector("#providerUpdate");
      if (!node) return;
      node.hidden = false;
      node.dataset.status = "checking";
      node.textContent = `Checking ${label} for updates…`;
      Promise.resolve().then(() => api(`/api/providers/${encodeURIComponent(provider)}/update`))
        .then((result) => {
          if (request !== generation) return;
          node.dataset.status = result.status;
          node.textContent = result.message || "Update check finished.";
          if (result.status === "update_available" && result.update_command) {
            node.append(document.createTextNode(` Update: ${result.update_command}`));
          }
        }).catch(() => {
          if (request !== generation) return;
          node.dataset.status = "unavailable";
          node.textContent = `Could not check ${label} for updates. You can keep working.`;
        });
    },
  };
})();
