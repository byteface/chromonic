"""Real GPU smoke test: creates a hidden window and verifies framebuffer pixels.

Requires a working display. No network requests or page scripts are involved.
Readback is only used by this test; normal browse2 frames never read pixels back.
"""
import glfw
import numpy as np
from myjs import Page
from chromonic.native_browser import GLRenderer, View


def main():
    print('Initializing GLFW', flush=True)
    if not glfw.init():
        raise RuntimeError('A working display is required')
    win = renderer = None
    try:
        glfw.window_hint(glfw.VISIBLE, False)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 2)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.STENCIL_BITS, 8)
        win = glfw.create_window(320, 240, 'chromonic GPU smoke', None, None)
        if not win:
            raise RuntimeError('Could not create a hidden window')
        glfw.make_context_current(win)
        renderer = GLRenderer()
        view = View(320, 240, loader=lambda _: Page(
            '<html><body style="margin:0;display:block;background-color:rgb(255,0,0);height:600px"></body></html>', run=False))
        assert view.navigate('https://example.com/')
        # Exercise the native Cocoa -> GLFW character callback, not just a
        # direct call to the editor. Optional test dependency, not a runtime
        # dependency of browse2 (other platforms still run the GPU checks).
        import sys
        if sys.platform == 'darwin':
            try:
                import objc
                import AppKit
            except ImportError:
                print('SKIP Cocoa input probe: PyObjC is not installed', flush=True)
            else:
                native = objc.objc_object(c_void_p=glfw.get_cocoa_window(win))
                view.focus_address()
                glfw.set_char_callback(win, lambda w, code: view.type_text(chr(code)))
                for text in 'example.com':
                    event = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                        AppKit.NSKeyDown, (0, 0), 0, 0, native.windowNumber(), None,
                        text, text, False, 0)
                    native.sendEvent_(event)
                glfw.poll_events()
                assert view.address == 'example.com', 'Native character input did not reach the address editor'
                print('PASS: native Cocoa key events -> GLFW -> address editor', flush=True)
        renderer.draw(view, glfw.get_framebuffer_size(win))
        pixels = renderer.surface.toarray()
        assert np.count_nonzero(pixels[:, :, 0] != pixels[:, :, 2]) > 1000, 'Expected a red document'
        glfw.set_window_size(win, 400, 300)
        glfw.poll_events()
        view.resize(*glfw.get_window_size(win))
        view.scroll(100)
        renderer.draw(view, glfw.get_framebuffer_size(win))
        assert renderer.size == glfw.get_framebuffer_size(win)
        glfw.swap_buffers(win)
        print('PASS: GPU draw, pixel readback, resize, scroll, and buffer swap', flush=True)
    finally:
        if renderer is not None:
            renderer.close()
        if win is not None:
            glfw.destroy_window(win)
        glfw.terminate()


if __name__ == '__main__':
    main()
