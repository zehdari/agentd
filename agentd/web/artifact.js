// The dashboard's viewer passes its theme in the URL; opened on their own, pages follow the device.
const theme = new URLSearchParams(location.search).get("theme");
if (theme === "light" || theme === "dark") document.documentElement.style.colorScheme = theme;
