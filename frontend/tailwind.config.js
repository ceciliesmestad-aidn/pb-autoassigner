/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        slate: {
          925: "#0f172a",
          950: "#0a0f1c",
        },
      },
    },
  },
  plugins: [],
};
