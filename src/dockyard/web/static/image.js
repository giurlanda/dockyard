// An Image's page while it is built: show what the build prints, then reload once it has ended
// (the page then says whether the Image is ready, or why not).
(() => {
  "use strict";
  const building = document.querySelector("[data-follow-image]");
  const log = document.querySelector("[data-image-log]");
  if (!building || !log) return;

  const follow = async () => {
    try {
      const response = await fetch(building.dataset.followImage);
      if (response.status === 404) return; // deleted meanwhile
      if (response.ok) {
        const image = await response.json();
        log.textContent = image.log;
        log.scrollTop = log.scrollHeight;
        if (image.status !== "building") {
          location.reload();
          return;
        }
      }
    } catch {
      // Dockyard unreachable for a moment: keep trying
    }
    setTimeout(follow, 1000);
  };
  setTimeout(follow, 1000);
})();
