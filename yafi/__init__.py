import os
import sys


def main():
    from . import fan_curve

    # Applying the fan curve on boot doesn't need GTK, so skip loading it
    if fan_curve.APPLY_ARG in sys.argv:
        return fan_curve.run()

    # Register resources if not running in Flatpak
    if not os.environ.get('FLATPAK_ID'):
        from gi.repository import Gio
        resource = Gio.Resource.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'yafi.gresource'))
        resource._register()

    from .main import main as gui_main
    return gui_main()
