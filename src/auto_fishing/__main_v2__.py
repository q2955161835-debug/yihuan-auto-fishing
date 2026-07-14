from auto_fishing.app import Application
from auto_fishing.product import v2_profile


if __name__ == "__main__":
    Application(profile=v2_profile()).run()
