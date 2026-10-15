// The new Project form: offer only the Git Credentials of the repository's provider, as soon as
// its address shows which provider it is on. Dockyard refuses any other one anyway.
(() => {
  "use strict";
  const repository = document.getElementById("repository");
  const credential = document.getElementById("git_credential");
  if (!repository || !credential) return;
  const providers = { "github.com": "github", "gitlab.com": "gitlab" };

  const offer = () => {
    let provider = null;
    try {
      provider = providers[new URL(repository.value.trim()).hostname.toLowerCase()] || null;
    } catch {
      // not an address yet: offer them all
    }
    for (const group of credential.querySelectorAll("optgroup[data-provider]")) {
      const offered = !provider || group.dataset.provider === provider;
      group.hidden = !offered;
      group.disabled = !offered;
    }
    if (credential.selectedOptions[0]?.parentElement.disabled) credential.value = "";
  };

  repository.addEventListener("input", offer);
  offer();
})();
