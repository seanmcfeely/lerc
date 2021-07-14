#!/usr/bin/env python3
#/data/home/carbonblack/env3/bin/python3

import os
import sys
import time
import argparse
import logging
import coloredlogs

from dateutil import tz

from lerc_control import lerc_api

logger = logging.getLogger("lerc_control."+__name__)

from cbapi import auth
from cbapi.response import *

## New Cb Stuff ##
from cbapi.psc import Device
from cbapi.psc.threathunter import CbThreatHunterAPI
from cbapi.errors import ConnectionError, UnauthorizedError, ServerError, ClientError

from cbinterface.cli import load_configured_environments
from cbinterface.config import get_default_cbapi_product, get_default_cbapi_profile
from cbinterface.helpers import input_with_timeout
from cbinterface.commands import ExecuteCommand, PutFile, GetFile, DeleteFile
from cbinterface.psc.sessions import CustomLiveResponseSessionManager

from cbinterface.psc.device import time_since_checkin, find_device_by_hostname, is_device_online

HOME_DIR = os.path.dirname(os.path.realpath(__file__)) 


def eastern_time(timestamp):
    eastern_timebase = tz.gettz('America/New_York')
    eastern_time = timestamp.replace(tzinfo=tz.gettz('UTC'))
    return eastern_time.astimezone(eastern_timebase).strftime('%Y-%m-%d %H:%M:%S.%f%z')


## Old Cb Response stuff ##
def go_live(sensor):
    start_time = time.time()
    timeout = 604800 # seven days
    current_day = 0
    lr_session = None
    while time.time() - start_time < timeout:
        try:
            lr_session = sensor.lr_session()
            logger.info("LR session started at {}".format(time.ctime()))
            break
        except TimeoutError:
            elapsed_time = time.time() - start_time
            if current_day != elapsed_time // 86400:
                current_day+=1
                logger.info("24 hours of timeout when polling for LR session")
                logger.info("Attempting LR session again on {} @ {}".format(args.sensor,
                                                                        time.ctime()))
    return lr_session


def deploy_lerc(sensor, install_cmd, environment='default', lerc_installer_path=None):

    if not isinstance(sensor, models.Sensor):
        logger.error("Cb models.Sensor object required.")
        return False

    hostname = sensor.hostname
    default_lerc_path = '/opt/lerc_control/lercSetup.msi'

    if lerc_installer_path is None:
        config = lerc_api.load_config(environment, required_keys=['client_installer'])
        if config.has_option(environment, 'client_installer'):
            lerc_installer_path = config[environment]['client_installer']
        else:
            lerc_installer_path = config['default']['client_installer']
        

    # create lerc session
    ls = lerc_api.lerc_session()
    # check and see if the client's already installed
    client = None
    try:
        client = ls.get_host(hostname)
    except:
        logger.warning("Can't reach the lerc control server")

    previously_installed = proceed_with_force = None
    if client:
        if client.status != 'UNINSTALLED':
            errmsg = "lerc server reports the client is already installed on a system with this hostname:\n{}"
            errmsg = errmsg.format(client)
            logger.warning(errmsg)
            proceed_with_force = input("Proceed with fresh install? (y/n) [n] ") or 'n'
            proceed_with_force = True if proceed_with_force == 'y' else False
            if not proceed_with_force:
                return None
        else:
            previously_installed = True
            logger.info("A client was previously uninstalled on this host: {}".format(client))

    lr_session = None
    try:
        logger.info(".. attempting to go live on the host with CarbonBlack..")
        lr_session = go_live(sensor)
    except Exception as e:
        logger.error("Failed to start Cb live response session on {}".format(hostname))
        return False

    with lr_session:

        if proceed_with_force:
            uninstall_cmd = "msiexec /x C:\Windows\Carbonblack\lercSetup.msi /quiet /qn /norestart /log C:\Windows\Carbonblack\lerc_Un-Install.log"
            logger.info("~ checking for installed lerc client on host..")
            result = lr_session.create_process("sc query lerc", wait_timeout=60, wait_for_output=True)
            result = result.decode('utf-8')
            logger.info("~ Got service query result:\n{}".format(result))
            if 'SERVICE_NAME' in result and 'lerc' in result:
                logger.info("~ attempting to uninstall lerc..")
                result = lr_session.create_process(uninstall_cmd, wait_timeout=60, wait_for_output=True)
                logger.info("~ Post uninstall service query:\n {}".format(lr_session.create_process("sc query lerc",
                                                                            wait_timeout=60, wait_for_output=True).decode('utf-8')))

        logger.info("~ dropping current Live Endpoint Response Client msi onto {}".format(hostname))
        logger.debug("Dropping this msi : {}".format(lerc_installer_path))
        filedata = None
        with open(lerc_installer_path, 'rb') as f:
            filedata = f.read()
        try:
            lr_session.put_file(filedata, "C:\\Windows\\Carbonblack\\lercSetup.msi")
        except Exception as e:
            if 'ERROR_FILE_EXISTS' in str(e):
                logger.info("~ lercSetup.msi already on host. Deleting..")
                lr_session.delete_file("C:\\Windows\\Carbonblack\\lercSetup.msi")
                lr_session.put_file(filedata, "C:\\Windows\\Carbonblack\\lercSetup.msi")
                #pass
            else:
                raise e

        logger.info("~ installing the lerc service")
        result = lr_session.create_process(install_cmd, wait_timeout=60, wait_for_output=True)

    def _get_install_log(logfile=None):
        logger.info("Getting install log..")
        logfile = logfile if logfile else r"C:\\Windows\\Carbonblack\\lerc_install.log"
        content = lr_session.get_file(logfile)
        with open(hostname+"_lerc_install.log", 'wb') as f:
            f.write(content)
        logger.info("wrote log file to {}_lerc_install.log".format(hostname))


    wait = 5 #seconds
    attempts = 6
    if previously_installed:
        attempts += attempts
    logger.info("~ Giving client up to {} seconds to check in with the lerc control server..".format(attempts*wait))

    for i in range(attempts):
        try:
            client = ls.get_host(hostname)
        except:
            logger.warning("Can't reach the lerc control server")
            break
        if client:
            if client.status != 'UNINSTALLED':
                break
        logger.info("~ giving the client {} more seconds".format(attempts*wait - wait*i))
        time.sleep(wait)

    if not client:
        logger.warning("failed to auto-confirm install with lerc server.")
        _get_install_log()
        return None
    elif previously_installed and client.status == 'UNINSTALLED':
        logger.warning("Failed to auto-confirm install. Client hasn't checked in.")
        _get_install_log()
        return False

    logger.info("Client installed on {} at '{}' - status={} - last check-in='{}'".format(hostname,
                                 client.install_date, client.status, client.last_activity))
    return client


#Get the right CarbonBlack sensor
def CbSensor_search(profile, hostname):
    cb = CbResponseAPI(profile=profile)
    sensor = None
    logger.info("Getting the sensor object from carbonblack")
    try:
        result = cb.select(Sensor).where("hostname:{}".format(hostname))
        if len(result) == 1:
            return result.one()
        if isinstance(result[0], models.Sensor):
            print()
            logger.warn("MoreThanOneResult searching for {0:s}".format(hostname))
            print("\nResult breakdown:")
            sensor_ids = []
            for s in result:
                sensor_ids.append(int(s.id))
                if int(s.id) == max(sensor_ids):
                    sensor = s
                print()
                print("Sensor object - {}".format(s.webui_link))
                print("-------------------------------------------------------------------------------\n")
                print("\tos_environment_display_string: {}".format(s.os_environment_display_string))
                print()
                print("\tstatus: {}".format(s.status))
                print("\tsensor_id: {}".format(s.id))
                print("\tlast_checkin_time: {}".format(s.last_checkin_time))
                print("\tnext_checkin_time: {}".format(s.next_checkin_time))
                print("\tsensor_health_message: {}".format(s.sensor_health_message))
                print("\tsensor_health_status: {}".format(s.sensor_health_status))
                print("\tnetwork_interfaces:")
            print()
            default_sid = max(sensor_ids)
            choice_string = "Which sensor do you want to use?\n"
            for sid in sensor_ids:
                choice_string += "\t- {}\n".format(sid)
            choice_string += "\nEnter one of the sensor ids above. Default: [{}]".format(default_sid)
            user_choice = int(input(choice_string) or default_sid)
            for s in result:
                if user_choice == int(s.id):
                    logger.info("Returning {} sensor".format(s))
                    return s
    except Exception as e:
        if sensor is None:
            logger.warning("A sensor by hostname '{}' wasn't found in this environment".format(hostname))
            #return False
        logger.error("{}".format(str(e)))
        return False
## end Cb Response functions ##

## new CBC deployment ##
def deploy_lerc_with_cbc(device: Device, install_command: str, lerc_installer_path: str, interactive: bool=False) -> lerc_api.Client:
    """Deploy LERC to a Carbon Black Cloud Device.
    
    Args:
        device: A Carbon Black Cloud Device
        install_command: the command that installs LERC.
        lerc_installer_path: path to a LERC installer package (MSI).
    Returns:
        An instance of the installed lerc_api.Client, if successful.
    """

    assert isinstance(device, Device)
    
    hostname = device.name[device.name.rfind('\\')+1:] if '\\' in device.name else device.name

    # HACK: save a copy of any the https_proxy var
    https_proxy = None
    if 'https_proxy' in os.environ:
        https_proxy = os.environ['https_proxy']

    # create lerc session
    ls = lerc_api.lerc_session()
    # check and see if the client's already installed
    client = None
    try:
        # NOTE: will remove proxy var from env
        client = ls.get_host(hostname)
        #os.environ['https_proxy'] = https_proxy
    except:
        logger.warning("Can't reach the lerc control server")

    previously_installed = proceed_with_force = None
    if client:
        if client.status != 'UNINSTALLED':
            logger.warning(f"lerc server reports the client is already installed on a system with this hostname:\n{client}")
            proceed_with_force = input_with_timeout("Proceed with fresh install? (y/n) [n] ", default='n')
            proceed_with_force = True if proceed_with_force == 'y' else False
            if not proceed_with_force:
                return None
        else:
            previously_installed = True
            logger.info("A client was previously uninstalled on this host: {}".format(client))

    cb = device._cb

    timeout = 1200  # default 20 minutes (same used by Cb)
    if not is_device_online(device):
        # Decision point: if the device is NOT online, give the analyst and option to wait
        logger.warning(f"{device.id}:{device.name} is offline.")
        wait = "y"
        if interactive:
            prompt = "Would you like to wait for the host to come online? (y/n) [y] "
            wait = input_with_timeout(prompt, default="y")
        wait = True if wait.lower() == "y" else False
        if not wait:
            return None
        timeout = 7
        if interactive:
            prompt = "How many days do you want to wait? [Default is 7 days] "
            timeout = input_with_timeout(prompt, default=7)
            if isinstance(timeout, str):
                timeout = int(timeout)
        if timeout > 30:
            logger.warning(f"{timeout} days is a long time. Restricting to max of 30 days.")
            timeout = 30

        # 86400 seconds in a day
        timeout = timeout * 86400

    logger.info(f"waiting for active session on device ...")
    session_manager = CustomLiveResponseSessionManager(cb, custom_session_keepalive=True)
    if not session_manager.wait_for_active_session(device, timeout=timeout):
        logger.error(f"reached timeout waiting for active session.")
        return False

      
    download = PutFile(lerc_installer_path, 'lercSetup.msi')
    execute = ExecuteCommand(install_command, wait_for_output=False, wait_timeout=60, wait_for_completion=True)

    logger.info(f"submitting commands to download and install lerc.") 
    if previously_installed:
        # delete any old msi package, just in-case
        session_manager.submit_command(DeleteFile('lercSetup.msi'), device)
    session_manager.submit_command(download, device)
    session_manager.submit_command(execute, device)
    session_manager.process_completed_commands() # wait

    # wait and anticipate the client check-in
    wait = 5 #seconds
    attempts = 6
    logger.info("~ Giving client up to {} seconds to check in with the lerc control server..".format(attempts*wait))

    for i in range(attempts):
        try:
            client = ls.get_host(hostname)
        except:
            logger.warning("Can't reach the lerc control server")
            break
        if client:
            if client.status != 'UNINSTALLED':
                break
        logger.info("~ giving the client {} more seconds".format(attempts*wait - wait*i))
        time.sleep(wait)

    if not client or client.status == 'UNINSTALLED':
        logger.warning("failed to auto-confirm install with lerc server.")
        if previously_installed:
            logger.warning("client never checked in.")
        logging.info("getting install log...")
        upload_log = GetFile('lerc_install.log', f"{hostname}_lerc_install.log")
        session_manager.submit_command(upload_log, device)
        session_manager.process_completed_commands()
        return False

    logger.info("Client installed on {} at '{}' - status={} - last check-in='{}'".format(hostname,
                                 client.install_date, client.status, client.last_activity))
    return client



def main(argv):

    # configure logging #
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(name)s - [%(levelname)s] %(message)s')
    coloredlogs.install(level='INFO', logger=logger)

    # load carbonblack environment profiles #
    configured_environments = load_configured_environments()
    environments = []
    # create human friendly options for the CLI
    for product, profiles in configured_environments.items():
        for profile in profiles:
            environments.append(f"{product}:{profile}")

    # chose the default environment
    default_product_name = get_default_cbapi_product()
    default_profile_name = get_default_cbapi_profile()
    default_environments = [env for env in environments if env.startswith(default_product_name)]
    default_environment = f"{default_product_name}:{default_profile_name}"
    default_environment = (
        default_environment if default_environments and default_environment in default_environments else environments[0]
    )

    # get the config items we need
    required_keys = ['client_installer', 'lerc_install_cmd']
    config = lerc_api.load_config(required_keys=required_keys)
    default_lerc_path = config['default']['client_installer'] #'/opt/lerc/lercSetup.msi'
    lerc_environments = config['default']['environments'].split(',') if 'environments' in config['default'] else ['default']

    parser = argparse.ArgumentParser(description="Use existing tools to install LERC")
    parser.add_argument("-d", "--debug", action="store_true", help="Turn on debug logging.")
    parser.add_argument('lerc_env', choices=lerc_environments, default='default', help='Specify the LERC environment the client should belong in.')
    parser.add_argument('--env', choices=environments, default=default_environment, help=f'Specify the Carbon Black environment to work with. default={default_environment}')
    parser.add_argument('hostname', help="the name of the host to deploy the client to")
    parser.add_argument('-p', '--package', default=default_lerc_path, help="the msi lerc package to install")
    args = parser.parse_args()

    print(time.ctime() + "... starting")

    if args.debug:
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
        coloredlogs.install(level="DEBUG", logger=logger)

    # ignore the proxy
    #del os.environ['https_proxy']

    product, profile = args.env.split(":", 1)

    install_command = config[args.lerc_env]['lerc_install_cmd']

    if product == "response":
        sensor = CbSensor_search(profile, args.hostname)

        result = deploy_lerc(sensor, install_command, environment=profile, lerc_installer_path=args.package)
        if result:
            print()
            print(result)
            print()
            return True
        return

    # new stuff - technically the old stuff works with the new stuff but agilitiy
    logger.debug(f"using '{profile}' profile via the configured '{product}' product.")
    cb = None
    try:
        if product == "response":
            cb = CbResponseAPI(profile=profile)
        elif product == "psc" or product == "cbc":
            cb = CbThreatHunterAPI(profile=profile)
    except ConnectionError as e:
        logger.critical(f"Couldn't connect to {product} {profile}: {e}")
    except UnauthorizedError as e:
        logger.critical(f"{e}")
    except ServerError as e:
        logger.critical(f"CB ServerError 😒 (try again) : {e}")
    except TimeoutError as e:
        logger.critical(f"TimeoutError waiting for CB server 🙄 (try again) : {e}")

    # try and get the device
    logger.info(f"searching for device...")
    device = None
    try:  # if device.id
        device = Device(cb, args.hostname)
    except ClientError:
        device = find_device_by_hostname(cb, args.hostname)

    if not device:
        logger.error("could not get device.")
        return 

    # assume cbc/psc
    result = deploy_lerc_with_cbc(device, install_command, default_lerc_path, interactive=True)
    if result:
        print(result)


if __name__ == "__main__":
    result = main(sys.argv[1:])
    if result != 1:
        print(time.ctime() + "...Done.")
    sys.exit(result)
