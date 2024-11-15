var userManager = null;

window.initOidc = () => {
  const Log = window.oidc.Log;
  const UserManager = window.oidc.UserManager;
  const WebStorageStateStore = window.oidc.WebStorageStateStore;

  Log.setLogger(console);
  Log.setLevel(Log.INFO);
  function debugAuthOut() {
    const debugAuthOut = document.getElementById("debugAuthOut");
    debugAuthOut.innerText = "";

    Array.prototype.forEach.call(arguments, function (msg) {
      if (msg instanceof Error) {
        msg = "Error: " + msg.message;
      } else if (typeof msg !== "string") {
        msg = JSON.stringify(msg, null, 2);
      }
      debugAuthOut.innerHTML += msg + "\r\n";
    });
  }

  // Extra params can be configured, eg:
  // window.OIDCSettings.extraQueryParams['hello'] = 'world'

  window.OIDCSettings.userStore = new WebStorageStateStore({
    store: window.localStorage,
  });
  userManager = new UserManager(window.OIDCSettings);

  userManager.events.addUserLoaded(function (user) {
    console.log("user loaded", user);
    userManager.getUser().then(
      function () {
        console.log("getUser loaded user after userLoaded event fired");
      },
      () => {}
    );
  });

  userManager.events.addUserUnloaded(function (e) {
    console.log("user unloaded");
  });

  /**
   * Testing UI Functions
   */

  const clearState = () => {
    userManager
      .clearStaleState()
      .then(() => {
        debugAuthOut("userManager: clearStateState success");
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

  const getUser = () => {
    userManager
      .getUser()
      .then((user) => {
        debugAuthOut("userManager: got user", user);
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

  const removeUser = () => {
    userManager
      .removeUser()
      .then(() => {
        debugAuthOut("userManager: user removed");
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

  const redirectSignin = () => {
    removeUser();
    clearState();
    userManager
      .signinRedirect()
      .then((user) => {
        debugAuthOut("userManager: signed in", user);
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

  const popupSignin = () => {
    removeUser();
    clearState();
    userManager
      .signinPopup()
      .then((user) => {
        debugAuthOut("userManager: signed in", user);
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

  // Automatically start the iFrame login on page load iframeSignin()
  const iframeSignin = () => {
    removeUser();
    clearState();
    userManager
      .signinSilent()
      .then((user) => {
        debugAuthOut("userManager: signed in", user);
      })
      .catch((err) => {
        console.error(err);
        debugAuthOut(err);
      });
  };

};
