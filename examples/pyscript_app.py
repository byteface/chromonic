# A <script type="text/python" src="pyscript_app.py"> file -- loaded and
# executed exactly like an inline <script type="text/python"> block would be
# (see chromonic/python/chromonic/pyscript.py:run_python_scripts). `document` and
# `window` are already in scope by the time this runs; nothing to import.

count = 0
button = document.querySelector("#hello")
status = document.querySelector("#status")


def clicked(event):
    global count
    count += 1
    status.textContent = f"Clicked {count} time{'s' if count != 1 else ''}"
    button.textContent = "Click me again" if count else "Click me"
    button.style.backgroundColor = "#38a169" if count % 2 == 0 else "#3182ce"


button.addEventListener("click", clicked)
