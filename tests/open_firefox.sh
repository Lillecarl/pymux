# Firefox on a red page, in a profile of its own: no first-run page and
# no default browser question to cover it. `open_a_window.sh` runs this
# on the server's compositor. Lillecarl/pymux#753.
profile=$(mktemp -d)
cat > "$profile/user.js" <<'PREFS'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("app.update.enabled", false);
PREFS
printf "<body style='margin:0;background:#ff0000'></body>" > "$profile/red.html"
HOME=$profile MOZ_ENABLE_WAYLAND=1 exec firefox --no-remote --new-instance --profile "$profile" "file://$profile/red.html"
