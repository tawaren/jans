#!/usr/bin/python3

import readline
import os
import sys
import time
import glob
import inspect
import zipfile
import shutil
import traceback
import code

from queue import Queue
queue = Queue()

os.environ['LC_ALL'] = 'C'

#first import paths and make changes if necassary
from setup_app import paths

#for example change log file location:
#paths.LOG_FILE = '/tmp/my.log'

from setup_app import static

# second import module base, this makes some initial settings
from setup_app.utils import base

from setup_app.utils.package_utils import PackageUtils
packageUtils = PackageUtils()
packageUtils.check_and_install_packages()

from setup_app.messages import msg
from setup_app.config import Config
from setup_app.utils.progress import jansProgress


from setup_app.setup_options import get_setup_options
from setup_app.utils import printVersion

from setup_app.test_data_loader import TestDataLoader
from setup_app.utils.properties_utils import propertiesUtils
from setup_app.utils.setup_utils import SetupUtils
from setup_app.utils.collect_properties import CollectProperties

from setup_app.installers.jans import JansInstaller
from setup_app.installers.httpd import HttpdInstaller
from setup_app.installers.opendj import OpenDjInstaller
from setup_app.installers.couchbase import CouchbaseInstaller
from setup_app.installers.jre import JreInstaller
from setup_app.installers.jetty import JettyInstaller
from setup_app.installers.jython import JythonInstaller
from setup_app.installers.oxauth import OxauthInstaller
from setup_app.installers.scim import ScimInstaller
from setup_app.installers.fido import FidoInstaller
#from setup_app.installers.oxd import OxdInstaller

if base.snap:
    try:
        open('/proc/mounts').close()
    except:
        print("Please execute the following command\n  sudo snap connect jans-server:mount-observe :mount-observe\nbefore running setup. Exiting ...")
        sys.exit()

# initialize config object
Config.init(paths.INSTALL_DIR)
Config.determine_version()

# we must initilize SetupUtils after initilizing Config
SetupUtils.init()

# get setup options from args
argsp, setupOptions = get_setup_options()

terminal_size = shutil.get_terminal_size()
tty_rows=terminal_size.lines 
tty_columns = terminal_size.columns

if not argsp.n:
    base.check_resources()


# pass progress indicator to Config object
Config.pbar = jansProgress


for key in setupOptions:
    setattr(Config, key, setupOptions[key])

jansInstaller = JansInstaller()
jansInstaller.initialize()

print()
print("Installing Janssen Server...\n\nFor more info see:\n  {}  \n  {}\n".format(paths.LOG_FILE, paths.LOG_ERROR_FILE))
print("Detected OS     :  {} {} {}".format('snap' if base.snap else '', base.os_type, base.os_version))
print("Janssen Version :  {}".format(Config.oxVersion))
print("Detected init   :  {}".format(base.os_initdaemon))
print("Detected Apache :  {}".format(base.determineApacheVersion()))
print()

setup_loaded = {}
if setupOptions['setup_properties']:
    base.logIt('%s Properties found!\n' % setupOptions['setup_properties'])
    setup_loaded = propertiesUtils.load_properties(setupOptions['setup_properties'])
elif os.path.isfile(Config.setup_properties_fn):
    base.logIt('%s Properties found!\n' % Config.setup_properties_fn)
    setup_loaded = propertiesUtils.load_properties(Config.setup_properties_fn)
elif os.path.isfile(Config.setup_properties_fn+'.enc'):
    base.logIt('%s Properties found!\n' % Config.setup_properties_fn+'.enc')
    setup_loaded = propertiesUtils.load_properties(Config.setup_properties_fn+'.enc')


collectProperties = CollectProperties()
if os.path.exists(Config.jans_properties_fn):
    collectProperties.collect()
    Config.installed_instance = True

    if argsp.csx:
        print("Saving collected properties")
        collectProperties.save()
        sys.exit()


if not Config.noPrompt and not Config.installed_instance and not setup_loaded:
    propertiesUtils.promptForProperties()

propertiesUtils.check_properties()

# initialize installers, order is important!
jreInstaller = JreInstaller()
jettyInstaller = JettyInstaller()
jythonInstaller = JythonInstaller()
openDjInstaller = OpenDjInstaller()
couchbaseInstaller = CouchbaseInstaller()
httpdinstaller = HttpdInstaller()
oxauthInstaller = OxauthInstaller()
fidoInstaller = FidoInstaller()
scimInstaller = ScimInstaller()
#oxdInstaller = OxdInstaller()


if Config.installed_instance:
    for installer in (openDjInstaller, couchbaseInstaller, httpdinstaller, 
                        oxauthInstaller, scimInstaller, fidoInstaller,
                        #oxdInstaller
                        ):

        setattr(Config, installer.install_var, installer.installed())

    propertiesUtils.promptForProperties()

    if not Config.addPostSetupService:
        print("No service was selected to install. Exiting ...")
        sys.exit()

if argsp.t or argsp.x:
    testDataLoader = TestDataLoader()
    testDataLoader.scimInstaller = scimInstaller

if argsp.x:
    print("Loading test data")
    testDataLoader.dbUtils.bind()
    testDataLoader.createLdapPw()
    testDataLoader.load_test_data()
    testDataLoader.deleteLdapPw()
    print("Test data loaded. Exiting ...")
    sys.exit()

print()
print(jansInstaller)

proceed = True
if not Config.noPrompt:
    proceed_prompt = input('Proceed with these values [Y|n] ').lower().strip()
    if proceed_prompt and proceed_prompt[0] !='y':
        proceed = False

#register post setup progress
class PostSetup:
    service_name = 'post-setup'
    install_var = 'installPostSetup'
    app_type = static.AppType.APPLICATION
    install_type = static.InstallOption.MONDATORY

jansProgress.register(PostSetup)
jansProgress.queue = queue


if argsp.shell:
    code.interact(local=locals())
    sys.exit()

def do_installation():

    jansProgress.before_start()
    jansProgress.start()

    try:
        jettyInstaller.calculate_selected_aplications_memory()

        if not Config.installed_instance:
            jansInstaller.configureSystem()
            jansInstaller.make_salt()
            oxauthInstaller.make_salt()

            if not base.snap:
                jreInstaller.start_installation()
                jettyInstaller.start_installation()
                jythonInstaller.start_installation()

            jansInstaller.copy_scripts()
            jansInstaller.encode_passwords()

            Config.ldapCertFn = Config.opendj_cert_fn
            Config.ldapTrustStoreFn = Config.opendj_p12_fn
            Config.encoded_ldapTrustStorePass = Config.encoded_opendj_p12_pass

            jansInstaller.prepare_base64_extension_scripts()
            jansInstaller.render_templates()
            jansInstaller.render_configuration_template()

            if not base.snap:
                jansInstaller.update_hostname()
                jansInstaller.set_ulimits()

            jansInstaller.copy_output()
            jansInstaller.setup_init_scripts()

            # Installing jans components

            if Config.wrends_install:
                openDjInstaller.start_installation()

            if Config.cb_install:
                couchbaseInstaller.start_installation()


        if (Config.installed_instance and 'installHttpd' in Config.addPostSetupService) or (not Config.installed_instance and Config.installHttpd):
            httpdinstaller.configure()

        if (Config.installed_instance and 'installOxAuth' in Config.addPostSetupService) or (not Config.installed_instance and Config.installOxAuth):
            oxauthInstaller.start_installation()

        if (Config.installed_instance and 'installFido2' in Config.addPostSetupService) or (not Config.installed_instance and Config.installFido2):
            fidoInstaller.start_installation()

        if (Config.installed_instance and 'installScimServer' in Config.addPostSetupService) or (not Config.installed_instance and Config.installScimServer):
            scimInstaller.start_installation()

        #if (Config.installed_instance and 'installOxd' in Config.addPostSetupService) or (not Config.installed_instance and Config.installOxd):
        #    oxdInstaller.start_installation()


        jansProgress.progress(PostSetup.service_name, "Saving properties")
        propertiesUtils.save_properties()
        time.sleep(2)

        for service in jansProgress.services:
            if service['app_type'] == static.AppType.SERVICE:
                jansProgress.progress(PostSetup.service_name, "Starting {}".format(service['name'].title()))
                time.sleep(2)
                service['object'].stop()
                service['object'].start()

        jansInstaller.post_install_tasks()

        jansProgress.progress(static.COMPLETED)

        print()
        for m in Config.post_messages:
            print(m)

    except:

        base.logIt("FATAL", True, True)

if proceed:
    do_installation()
    print('\n', static.colors.OKGREEN)
    msg_text = msg.post_installation if Config.installed_instance else msg.installation_completed.format(Config.hostname)
    print(msg_text)
    print('\n', static.colors.ENDC)
    # we need this for progress write last line
    time.sleep(2)

