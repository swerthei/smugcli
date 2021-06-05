# Interactive shell for running smugcli commands

import cmd
import os
import pathlib
import shlex
import sys
import re
import traceback

class SmugMugShell(cmd.Cmd):
  intro = 'Welcome to the SmugMug shell.   Type help or ? to list commands.\n'
  baseprompt = '(smugmug) '
  _cmd_list_re = re.compile(r'.*\{([a-z,]+)\}', re.DOTALL)

  def __init__(self, fs):
    cmd.Cmd.__init__(self)
    self._fs = fs
    self.user = fs.smugmug.get_auth_user()

  def do_exit(self, arg):
    'Exit the shell'
    return True
    
  def do_quit(self, arg):
    'Exit the shell'
    return True

  def do_lcd(self, arg):
    'Change local directory'
    if arg:
      d = arg
    else:
      d = os.environ['HOMEDRIVE'] + os.environ['HOMEPATH']

    try:
      os.chdir(d)
    except FileNotFoundError:
      print(f'{d} not found or not a directory')
    except:
      raise

    print(os.getcwd())
  
  def do_lpwd(self, arg):
    'Print local current directory'
    print(os.getcwd())

  def do_lls(self, arg):
    'List contents of local directory'
    if arg:
      d = arg
    else:
      d = os.getcwd()

    try:
      g = pathlib.Path(d).glob('*')
      for f in g:
        suffix = ''
        if f.is_dir(): suffix = '/'
        print(str(f) + suffix)
    except FileNotFoundError:
      print(f'{d} not found or not a directory')
    except:
      raise
    
  def setprompt(self):
    self.prompt = f'({self.user}) {self._fs.cwd}: '
  
  def preloop(self):
    self.setprompt()

  def emptyline(self):
    return
  
  def postcmd(self, stop, line):
    self.setprompt()
    return stop
  
  @classmethod
  def set_parser(cls, parser):
    usage = parser.format_usage()
    commands = SmugMugShell._cmd_list_re.match(usage).group(1).split(',')

    def do_handler(command):
      def handler(self, args):
        try:
          parsed = parser.parse_args([command] + shlex.split(args))
          parsed.func(parsed)
        except SystemExit:
          pass
        except:
          traceback.print_exc()
          pass
      return handler

    def help_handler(command):
      def handler(self):
        try:
          parser.parse_args([command, '--help'])
        except:
          pass
      return handler

    for command in commands:
      setattr(cls, 'do_' + command, do_handler(command))
      setattr(cls, 'help_' + command, help_handler(command))
