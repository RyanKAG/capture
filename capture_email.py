"""Email a PNG of one macOS or Windows window on an interval or daily schedule."""

import argparse
import configparser
import logging
import sys
import tempfile
from datetime import datetime
from email import policy
from pathlib import Path

from scheduler import IntervalSchedule, load_schedule, run_schedule
from screenshot_mail import load_mail_settings, screenshot_message, send_screenshot


def read_config(path):
    config = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as file:
            config.read_file(file, source=str(path))
    except FileNotFoundError as exc:
        raise ValueError(
            f"Config file missing at {path}. Copy config.example.ini to config.ini and edit it."
        ) from exc
    return config


def choose_window(titles):
    if not titles:
        raise ValueError("Open the window you want to capture and run the test again.")
    for number, title in enumerate(titles, start=1):
        print(f"  {number}. {title}")
    choice = input("Window number: ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(titles):
        raise ValueError("Select a window number from the list.")
    return titles[int(choice) - 1]


def test_mail_config(titles, *, mail_app=False):
    if mail_app:
        app = "Apple Mail" if sys.platform == "darwin" else "classic Outlook"
        print(f"Send one screenshot to yourself through your account in {app}.")
        address_prompt = f"Your email address in {app}: "
    else:
        print("Send one window screenshot to your own Gmail inbox.")
        address_prompt = "Your Gmail or Google Workspace email address: "
    title = choose_window(titles)
    address = input(address_prompt).strip()
    email = {"sender": address, "recipients": address}
    if mail_app:
        email["transport"] = "mail_app"
    else:
        email.update(host="smtp.gmail.com", port="587", security="starttls", username=address)
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict({
        "window": {"title": title},
        "email": email,
    })
    return config


def timed_test(titles, capture_window):
    print("Choose a window for two captures, 15 seconds apart.")
    title = choose_window(titles)
    app = "Apple Mail" if sys.platform == "darwin" else "classic Outlook"
    print(f"Press Return for local email files, or enter your address in {app} to email both to yourself.")
    address = input("Email address (optional): ").strip()
    if address:
        config = configparser.ConfigParser(interpolation=None)
        config.read_dict({"email": {"transport": "mail_app", "sender": address, "recipients": address}})
        settings = load_mail_settings(config)

        def deliver(png, captured_at):
            send_screenshot(settings, title, captured_at, png)
            print(f"Submitted a screenshot to {address} through {app}.", flush=True)
    else:
        folder = Path(tempfile.mkdtemp(prefix="screenshot-test-", dir=Path.cwd()))
        print(f"Saving email files in {folder}", flush=True)

        def deliver(png, captured_at):
            message = screenshot_message("capture@example.test", ("inbox@example.test",), title, captured_at, png)
            path = folder / f"{captured_at:%H-%M-%S-%f}.eml"
            path.write_bytes(message.as_bytes(policy=policy.SMTP))
            print(f"Saved {path.name}", flush=True)

    def take_sample():
        png = capture_window(title)
        deliver(png, datetime.now().astimezone())

    run_schedule(IntervalSchedule(0.25), take_sample, max_runs=2)
    print("Both captures completed. The test has stopped.")


def run(args):
    if sys.platform == "darwin":
        from mac_window_capture import capture_window, list_windows
    elif sys.platform == "win32":
        from window_capture import capture_window, list_windows
    else:
        raise RuntimeError("Run this script in the macOS or Windows session that owns the window.")

    if args.list_windows:
        for title in list_windows():
            print(title)
        return
    if args.test_schedule:
        timed_test(list_windows(), capture_window)
        return

    testing = args.gmail_test or args.mail_app_test
    if testing:
        config = test_mail_config(list_windows(), mail_app=args.mail_app_test)
    else:
        config = read_config(args.config)
    title = config.get("window", "title").strip()
    if not title:
        raise ValueError("Set [window] title to a title shown by --list-windows.")

    if args.preview:
        png = capture_window(title)
        with args.preview.open("xb") as file:
            file.write(png)
        logging.info("Saved preview to %s", args.preview)
        return

    schedule = None if args.once or testing else load_schedule(config)
    settings = load_mail_settings(config, use_password_env=not args.gmail_test)

    def capture_and_send():
        png = capture_window(title)
        captured_at = datetime.now().astimezone()
        send_screenshot(settings, title, captured_at, png)
        logging.info("Submitted the screenshot of %r for email delivery.", title)

    if schedule is None:
        capture_and_send()
        return

    def scheduled_job():
        try:
            capture_and_send()
        except (OSError, ValueError, RuntimeError) as exc:
            logging.error("Scheduled attempt failed. %s", exc)

    logging.info("Schedule active for %r. Press Ctrl+C to stop.", title)
    run_schedule(schedule, scheduled_job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.ini"),
        help="configuration file (default: config.ini beside this script)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-windows", action="store_true", help="print target window titles")
    mode.add_argument("--preview", type=Path, metavar="FILE", help="save one local PNG to a new file")
    mode.add_argument("--once", action="store_true", help="capture and email once, then exit")
    mode.add_argument("--test-schedule", action="store_true", help="take two timed captures as local emails or send them to yourself")
    mode.add_argument(
        "--gmail-test", action="store_true",
        help="choose a window and send it once to your own Gmail inbox (no config file needed)",
    )
    mode.add_argument(
        "--mail-app-test", action="store_true",
        help="choose a window and send it to yourself through Apple Mail or classic Outlook",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args)
    except KeyboardInterrupt:
        logging.info("Stopped.")
    except (configparser.Error, OSError, ValueError, RuntimeError, KeyError, ImportError, EOFError) as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
