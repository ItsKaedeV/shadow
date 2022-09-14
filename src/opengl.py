from threading import Thread
from OpenGL import GL as gl

import xcffib
import xcffib.xproto

from elements import *
from utils import *
from shader import Shader

from PboDownloader import PboDownloader

from scipy import linalg

import numpy as np

import contextlib
import logging
import ctypes
import time
import glfw
import sys

from config import BackgroundMode, QualityMode, Config

log = logging.getLogger(__name__)

@contextlib.contextmanager
def create_main_window(conn):
    try:
        log.debug('requiring modern OpenGL without any legacy features')
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.DOUBLEBUFFER, True)
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, True)

        if Config.BACKGROUND_MODE == BackgroundMode.BACKGROUND:
            glfw.window_hint(glfw.DECORATED, False)
            glfw.window_hint(glfw.FOCUSED, False)

        if Config.BACKGROUND_MODE == BackgroundMode.ROOT:
            glfw.window_hint(glfw.VISIBLE, glfw.FALSE)

        log.debug('opening window')
        window = glfw.create_window(Config.WIDTH, Config.HEIGHT, "Show", None, None)
        if not window:
            log.error('failed to open GLFW window.')
            sys.exit(2)

        if Config.BACKGROUND_MODE == BackgroundMode.BACKGROUND:
            log.debug('changed window typehint to background')
            set_window_to_background(conn, glfw.get_x11_window(window))

        if Config.BACKGROUND_MODE != BackgroundMode.ROOT and Config.OPACITY < 1:
            log.debug('changed opacity of window')
            glfw.set_window_opacity(window, Config.OPACITY)

        glfw.make_context_current(window)

        yield window

    finally:
        log.debug('terminating window context')
        glfw.terminate()

@contextlib.contextmanager
def create_vertex_array_object():
    log.debug('creating and binding the vertex array (VAO)')
    vertex_array_id = gl.glGenVertexArrays(1)
    try:
        gl.glBindVertexArray(vertex_array_id)
        yield
    finally:
        log.debug('cleaning up vertex array')
        gl.glDeleteVertexArrays(1, [vertex_array_id])

@contextlib.contextmanager
def create_vertex_buffer():
    with create_vertex_array_object():
        # 2 triangles
        vertex_data = [ 1, -1,   -1, -1,   -1,  1,
                       -1,  1,    1,  1,    1, -1 ]

        attr_id = 0  # No particular reason for 0,
                     # but must match the layout location in the shader.

        log.debug('creating and binding the vertex buffer (VBO)')
        vertex_buffer = gl.glGenBuffers(1)
        try:
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vertex_buffer)

            array_type = (gl.GLfloat * len(vertex_data))
            gl.glBufferData(gl.GL_ARRAY_BUFFER,
                            len(vertex_data) * ctypes.sizeof(ctypes.c_float),
                            array_type(*vertex_data),
                            gl.GL_STATIC_DRAW)

            log.debug('setting the vertex attributes')
            gl.glVertexAttribPointer(
               attr_id,            # attribute 0.
               2,                  # components per vertex attribute
               gl.GL_FLOAT,        # type
               False,              # to be normalized?
               0,                  # stride
               None                # array buffer offset
            )
            gl.glEnableVertexAttribArray(attr_id)  # use currently bound VAO
            yield
        finally:
            log.debug('cleaning up buffer')
            gl.glDisableVertexAttribArray(attr_id)
            gl.glDeleteBuffers(1, [vertex_buffer])

def create_frametexture():
    texture = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)

    width = int(Config.WIDTH * Config.QUALITY)
    height = int(Config.HEIGHT * Config.QUALITY)

    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)

    gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)

    if Config.QUALITY_MODE == QualityMode.PIXEL:
        gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)

    return texture


def create_framebuffer():
    # create a new framebuffer
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)

    # create a new texture
    texture = create_frametexture()

    # apply texture to framebuffer
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, texture, 0)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    return texture, fbo


def normalized(v):
    norm = linalg.norm(v)
    return v / norm if norm > 0 else v

def perspective(fov, aspect, near, far):
    n, f = near, far
    t = np.tan((fov * np.pi / 180) / 2) * near
    b = - t
    r = t * aspect
    l = b * aspect
    assert abs(r - l) > 0
    assert abs(t - b) > 0
    assert abs(n - f) > 0
    return np.array((
        ((2*n)/(r-l),           0,           0,  0),
        (          0, (2*n)/(t-b),           0,  0),
        ((r+l)/(r-l), (t+b)/(t-b), (f+n)/(n-f), -1),
        (          0,           0, 2*f*n/(n-f),  0)))

def look_at(eye, target, up):
    zax = normalized(eye - target)
    xax = normalized(np.cross(up, zax))
    yax = np.cross(zax, xax)
    x = - xax.dot(eye)
    y = - yax.dot(eye)
    z = - zax.dot(eye)
    return np.array(((xax[0], yax[0], zax[0], 0),
                     (xax[1], yax[1], zax[1], 0),
                     (xax[2], yax[2], zax[2], 0),
                     (     x,      y,      z, 1)))

def create_mvp(eyeX, eyeY, eyeZ):
    fov, near, far = 90, 0.1, 1000
    eye = np.array((eyeX, eyeY, eyeZ))
    target, up = np.array((0,0,0)), np.array((0,1,0))
    projection = perspective(fov, 1, near, far)
    view = look_at(eye, target, up)
    model = np.identity(4)
    mvp = model @ view @ projection
    return mvp.astype(np.float32)

def main_loop(conn, window, files):
    old = time.time()
    screen = conn.get_setup().roots[0]

    texture, fbo = create_framebuffer()
    prevTexture = create_frametexture()

    Config.mvp = create_mvp(0, 0, 1.9)

    components = []

    for file in files:
        component = create_component_from_file(file)
        if component is not None:
            components.append(component)

    shader_texture = Shader({
        gl.GL_VERTEX_SHADER: '''\
            #version 330 core
            layout(location = 0) in vec2 pos;
            out vec2 coords;
            uniform mat4 mvp;
            uniform bool swap;

            void main() {
              vec4 position = mvp * vec4(pos, 1, 1);

              if (swap) {
                position *= vec4(1, -1, 1, 1);
              }

              gl_Position = position;
              coords = pos * vec2(0.5) + vec2(0.5);
            }
            ''',
        gl.GL_FRAGMENT_SHADER: '''\
            #version 330 core
            uniform sampler2D tex;
            uniform vec2 resolution;

            in vec2 coords;

            void main() {
              gl_FragColor = texture(tex, coords);
            }
            ''',
    })

    # Pixmap used to set root background image
    pixmap = conn.generate_id()
    conn.core.CreatePixmap(
        screen.root_depth,
        pixmap,
        screen.root,
        Config.WIDTH,
        Config.HEIGHT,
    )

    # GC used for converting OpenGL's framebuffer to a pixmap
    gc = conn.generate_id()
    conn.core.CreateGC(
        gc,
        pixmap,
        0,
        None,
    )

    frames = 0
    passed = 0
    elapsed = 0

    pbo = PboDownloader(gl.GL_BGRA, Config.WIDTH, Config.HEIGHT, 1)

    try:
        while glfw.get_key(window, glfw.KEY_ESCAPE) != glfw.PRESS and not glfw.window_should_close(window):

            # Calculate framerate limit
            now = time.time()
            dt = now - old
            time.sleep(max(1 / Config.FRAMELIMIT - dt, 0))

            # calculate deltatime
            now = time.time()
            dt = now - old
            old = now

            update_time = dt * Config.SPEED
            elapsed += update_time
            passed += dt
            frames += 1

            if passed >= 1:
                print(frames)
                passed = 0
                frames = 0

            # If window size changes, update width, height and framebuffer
            nwidth, nheight = glfw.get_window_size(window)

            if nwidth != Config.WIDTH or nheight != Config.HEIGHT:
                # width and height should get to small
                Config.WIDTH = max(nwidth, 10)
                Config.HEIGHT = max(nheight, 10)

                # Delete existing framebuffer and create a new one with new scale
                gl.glDeleteFramebuffers(1, fbo)
                gl.glDeleteTextures(1, texture)
                texture, fbo = create_framebuffer()

                gl.glDeleteTextures(1, prevTexture)
                prevTexture = create_frametexture()


            # Render shader background animation to framebuffer with less quality if set
            gl.glViewport(0, 0, int(Config.WIDTH * Config.QUALITY), int(Config.HEIGHT * Config.QUALITY))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)

            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

            # Update all components
            window.prevTexture = prevTexture
            window.texture = texture
            for component in components:
                component.render(update_time, window)


            # Copy rendered framebuffer to prevTexture which is being used for "prevBuffer" sampler
            gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0);
            gl.glActiveTexture(gl.GL_TEXTURE0);
            gl.glBindTexture(gl.GL_TEXTURE_2D, prevTexture);
            gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 0, 0, int(Config.WIDTH * Config.QUALITY), int(Config.HEIGHT * Config.QUALITY));


            # Draw framebuffer with normal size to window
            gl.glViewport(0, 0, Config.WIDTH, Config.HEIGHT)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)  # unbind FBO to set the default framebuffer
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture) # color attachment texture

            shader_texture.bind()
            gl.glUniform2f(shader_texture.get_uniform("resolution"), Config.WIDTH, Config.HEIGHT)
            gl.glUniform1i(shader_texture.get_uniform("swap"), Config.BACKGROUND_MODE == BackgroundMode.ROOT) # root mode needs to be swapped vertically 
            gl.glUniformMatrix4fv(shader_texture.get_uniform("mvp"), 1, False, Config.mvp)


            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)

            # Convert framebuffer to pixmap and set it to root window
            if Config.BACKGROUND_MODE == BackgroundMode.ROOT:
                def pbo_to_screen():
                    PutImage(conn, xcffib.xproto.ImageFormat.ZPixmap, pixmap, gc, Config.WIDTH, Config.HEIGHT, 0, 0, 0, screen.root_depth, pbo.pixels)
                    set_wallpaper_pixmap(conn, screen, pixmap)

                pbo.download()
                Thread(target=pbo_to_screen,args=()).start()
            else:
                glfw.swap_buffers(window) # Apply changes to window

            glfw.poll_events()
    except KeyboardInterrupt:
        log.debug("Exit signal received")

    # Free buffers
    gl.glDeleteFramebuffers(1, fbo)
    gl.glDeleteTextures(1, texture)

    del shader_texture

    for component in components:
        component.cleanup()

    conn.core.FreePixmap(pixmap)
    conn.core.FreeGC(gc)
