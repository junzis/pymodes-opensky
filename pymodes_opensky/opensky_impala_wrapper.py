import os
import re
import pandas as pd
import configparser
import logging
import warnings
from io import StringIO
from typing import Iterable
from pathlib import Path
from pymodes_opensky.ssh_client import SSHClient

# logging.basicConfig(level=logging.DEBUG)
# logger = logging.getLogger(__name__)
# logger.setLevel(logging.DEBUG)

warnings.filterwarnings(action="ignore", module=".*paramiko.*")

# check login config file
homedir = str(Path.home())
config_path = homedir + "/.config/pymodes_opensky/secret.conf"

if not os.path.exists(config_path):
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        f.write("[default]\n")
        f.write("server = data.opensky-network.org\n")
        f.write("port = 2230\n")
        f.write("username = \n")
        f.write("password = \n")

config = configparser.ConfigParser()
config.read(homedir + "/.config/pymodes_opensky/secret.conf")

SERVER = config.get("default", "server")
PORT = config.get("default", "port")
USERNAME = config.get("default", "username")
PASSWORD = config.get("default", "password")

if not USERNAME or not PASSWORD:
    raise RuntimeError(
        "Opensky Impala username and password are empty in {}".format(config_path)
    )


class OpenskyImpalaWrapper(SSHClient):
    """docstring for OpenskyImpalaWrapper."""

    def __init__(self):
        SSHClient.__init__(self)
        self.connect_opensky()

    def connect_opensky(self):
        self.connect(
            SERVER,
            port=PORT,
            username=USERNAME,
            password=PASSWORD,
            look_for_keys=False,
            allow_agent=False,
            compress=True,
        )

    def disconnect_opensky(self):
        self.disconnect()

    def check_and_reconnect(self):
        try:
            transport = self.get_transport()
            transport.send_ignore()
        except EOFError:
            print("Connection lost, reconnecting...")
            self.connect_opensky()

    def query(self, type, start, end, icao24=None, bound=None, countfirst=True):
        start = pd.Timestamp(start, tz="utc").timestamp()
        end = pd.Timestamp(end, tz="utc").timestamp()

        hour_start = start // 3600 * 3600
        hour_end = (end // 3600 + 1) * 3600

        if type == "adsb":
            table = "state_vectors_data4"
            time_col = "time"
        elif type == "raw":
            table = "rollcall_replies_data4"
            time_col = "mintime"

        icao_filter = ""
        if isinstance(icao24, str):
            icao_filter += "AND icao24='{}' ".format(icao24.lower())
        elif isinstance(icao24, Iterable):
            icao24s = ",".join(["'" + x.lower() + "'" for x in icao24])
            icao_filter += "AND icao24 in ({}) ".format(icao24s)

        bound_filter = ""
        if bound is None:
            pass
        elif len(bound) != 4:
            raise RuntimeError("bound format must be [lat1, lon1, lat2, lon2]")
        else:
            lat1, lon1, lat2, lon2 = bound
            lat_min = min((lat1, lat2))
            lat_max = max((lat1, lat2))

            bound_filter += "AND lat>={} AND lat<={} ".format(lat_min, lat_max)

            if lon1 < lon2:
                bound_filter += "AND lon>={} AND lon<={} ".format(lon1, lon2)
            else:
                bound_filter += "AND lon>={} OR lon<={} ".format(lon1, lon2)

        cmd = (
            "SELECT * FROM {} WHERE ".format(table)
            + "hour>={} ".format(hour_start)
            + "AND hour<{} ".format(hour_end)
            + "AND {}>={} ".format(time_col, start)
            + "AND {}<{} ".format(time_col, end)
            + icao_filter
            + bound_filter
        )

        if countfirst:
            # check how many records are related to the query
            count_cmd = cmd.replace("*", "COUNT(*)")
            print("**Obtaining details of the query...")
            logging.info("Sending count request: [" + count_cmd + "]")

            self.check_and_reconnect()
            output = self.shell("-q " + count_cmd)
            count = int(re.findall(r"\d+", output)[0])
            print("**OpenSky Impala: {} of records found.".format(count))

            if count == 0:
                print("**No record found.")
                return None

        # sending actual query
        print("**Fetching records...")
        logging.info("Sending query request: [" + cmd + "]")

        self.check_and_reconnect()
        output = self.shell("-q " + cmd)

        logging.info("Processing query result.")
        sio = StringIO()
        for i, line in enumerate(output.split("\n")):
            if "|" not in line:
                # keep only table rows
                continue
            if "hour" in line and i > 10:
                # skip header row, after first occurance
                continue
            new_line = re.sub(r" *\| *", ",", line)[1:-1]
            sio.write(new_line + "\n")

        sio.seek(0)
        df = pd.read_csv(sio, dtype={"icao24": str})

        if "time" in df.columns.tolist():
            df = df.sort_values("time")
        elif "mintime" in df.columns.tolist():
            df = df.sort_values("mintime")

        print("**Records downloaded.")

        return df