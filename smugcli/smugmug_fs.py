from . import persistent_dict
from . import task_manager  # Must be included before hachoir so stdout override works.
from . import thread_pool
from . import thread_safe_print

import six
import collections
import datetime
import glob
import re
import fnmatch

if six.PY2:
  from hachoir_metadata import extractMetadata
  from hachoir_parser import guessParser
  from hachoir_core.stream import StringInputStream
  from hachoir_core import config as hachoir_config
else:
  from hachoir.metadata import extractMetadata
  from hachoir.parser import guessParser
  from hachoir.stream import StringInputStream
  from hachoir.core import config as hachoir_config

from six.moves import input
import itertools
import json
import hashlib
import os
import requests
from six.moves import urllib

hachoir_config.quiet = True

DEFAULT_MEDIA_EXT = ['gif', 'jpeg', 'jpg', 'mov', 'mp4', 'png', 'heic']
VIDEO_EXT = ['mov', 'mp4']


class Error(Exception):
  """Base class for all exception of this module."""


class RemoteDataError(Error):
  """Error raised when the remote structure is incompatible with SmugCLI."""


class SmugMugLimitsError(Error):
  """Error raised when SmugMug limits are reached (folder depth, size. etc.)"""


class UnexpectedResponseError(Error):
  """Error raised when encountering unexpected data returned by SmugMug."""


class SmugMugFS(object):
  def __init__(self, smugmug):
    self._smugmug = smugmug
    self._aborting = False
    self._cwd = os.sep

    # Pre-compute some common variables.
    self._media_ext = [
      ext.lower() for ext in
      self.smugmug.config.get('media_extensions', DEFAULT_MEDIA_EXT)]

  @property
  def smugmug(self):
    return self._smugmug

  @property
  def cwd(self):
    return self._cwd

  def abort(self):
    self._aborting = True

  def get_root_node(self, user):
    return self._smugmug.get_root_node(user)

  def glob(self, user, path, directory, re_match=False):
    # Globbing algorithm
    # split path into pathlist
    # worklist = [rootnode]
    # for pathitem in pathlist:
    #   worklist2 = []
    #   for workitem in worklist:
    #     worklist2.extend(children of workitem that match pathitem)
    #   worklist = worklist2
    current_node = self.get_root_node(user)
    pathlist = []
    if user == self._smugmug.get_auth_user() and (len(path) == 0 or path[0] != os.sep):
      pathlist.extend(list(filter(bool, self._cwd.split(os.sep))))
    pathlist.extend(list(filter(bool, path.split(os.sep))))
    worklist = [self.get_root_node(user)]
    for pathitem in pathlist:
      worklist2 = []
      for workitem in worklist:
        regex = re.compile(pathitem if re_match else fnmatch.translate(pathitem))
        worklist2.extend([node for node in workitem.get_children() if regex.fullmatch(node.name)])
      worklist = worklist2
    return worklist
  
  def resolve_multinodes(self, user, path, directory, re_match=False):
    matched_nodes, unmatched_dirs = self.path_to_node(user, path)
    if unmatched_dirs:
      if len(unmatched_dirs) > 1:
        print('"%s" not found in "%s".' % (
          unmatched_dirs[0], matched_nodes[-1].path))
        return []

      regex = re.compile(unmatched_dirs[0] if re_match else fnmatch.translate(unmatched_dirs[0]))
      ret = [node for node in matched_nodes[-1].get_children() if regex.fullmatch(node.name)]
      if len(ret) == 0:
        print('"%s" not found in "%s".' % (
          unmatched_dirs[0], matched_nodes[-1].path))
      return ret

    node = matched_nodes[-1]
    return [node] if 'FileName' in node or directory else node.get_children()
  
  def path_to_node(self, user, path):
    current_node = self.get_root_node(user)
    parts = []
    if user == self._smugmug.get_auth_user() and (len(path) == 0 or path[0] != os.sep):
      parts.extend(list(filter(bool, self._cwd.split(os.sep))))
    parts.extend(list(filter(bool, path.split(os.sep))))
    nodes = [current_node]
    return self._match_nodes(nodes, parts)

  def _match_nodes(self, matched_nodes, dirs):
    unmatched_dirs = collections.deque(dirs)
    for dir in dirs:
      if dir == '..':
        if len(matched_nodes) > 1:
          matched_nodes.pop(-1)
      elif dir == '.':
        pass
      else:
        child_node = matched_nodes[-1].get_child(dir)
        if not child_node:
          break
        matched_nodes.append(child_node)

      unmatched_dirs.popleft()
      
    return matched_nodes, list(unmatched_dirs)

  def _match_or_create_nodes(self, matched_nodes, dirs, node_type, privacy):
    folder_depth = len(matched_nodes) + len(dirs)
    folder_depth -= 1 if node_type == 'Album' else 0
    if folder_depth >= 7:  # matched_nodes include an extra node for the root.
      raise SmugMugLimitsError(
        'Cannot create "%s", SmugMug does not support folder more than 5 level '
        'deep.' % os.sep.join([matched_nodes[-1].path] + dirs))

    all_nodes = list(matched_nodes)
    for i, dir in enumerate(dirs):
      params = {
        'Type': node_type if i == len(dirs) - 1 else 'Folder',
        'Privacy': privacy,
      }
      all_nodes.append(all_nodes[-1].get_or_create_child(dir, params))
    return all_nodes

  def get(self, url):
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(query)
    #print(f'url      = {url}')
    #print(f'scheme   = {scheme}')
    #print(f'netloc   = {netloc}')
    #print(f'path     = {path}')
    #print(f'query    = {query}')
    #print(f'fragment = {fragment}')
    result = self._smugmug.get_json(path, params=params)
    print(json.dumps(result, sort_keys=True, indent=2, separators=(',', ': ')))

  def ignore_or_include(self, paths, ignore):
    files_by_folder = collections.defaultdict(list)
    for folder, file in [os.path.split(path) for path in paths]:
      files_by_folder[folder].append(file)

    for folder, files in six.iteritems(files_by_folder):
      if not os.path.isdir(folder or '.'):
        print('Can\'t find folder "%s".' % folder)
        return
      for file in files:
        full_path = os.path.join(folder, file)
        if not os.path.exists(full_path):
          print('"%s" doesn\'t exists.' % full_path)
          return

      configs = persistent_dict.PersistentDict(os.path.join(folder, '.smugcli'))
      original_ignore = configs.get('ignore', [])
      if ignore:
        updated_ignore = list(set(original_ignore) | set(files))
      else:
        updated_ignore = list(set(original_ignore) ^ (set(files) &
                                                      set(original_ignore)))
      configs['ignore'] = updated_ignore

  ftypes = {
    'Folder': 'F',
    'Album': 'A',
    'System Album': 'S'
    }

  def printnode(self, node, details, bare, fullpath):
    printname = node.path if fullpath else node.name
    if details:
      print(json.dumps(node.json, sort_keys=True, indent=2,
                       separators=(',', ': ')))
    elif bare:
      print(printname)
    else:
      if 'Type' in node:
        if node['Type'] in self.ftypes:
          abbrev = self.ftypes[node['Type']]
        else:
          abbrev = 'Unknown Type'
      elif 'FileName' in node:
        if node['IsVideo']:
          abbrev = 'V'
        else:
          abbrev = 'P'
      else:
        abbrev = 'U'
      print(f'{abbrev} {printname}')

  def process_children(self, node, recurse, details, bare, fullpath, print_header, processfn):
    if print_header:
      print(f'\n{node.path}:')
    children = node.get_children()
    for child in children:
      processfn(child, details, bare, fullpath)

    if recurse:
      for child in children:
        if 'Type' in child:
          self.process_children(child, recurse, details, bare, fullpath, True, processfn)
    
  def ls(self, user, path, directory, re_match, recurse, details, bare):
    user = user or self._smugmug.get_auth_user()
    nodelist = self.glob(user, path, directory, re_match)
    #nodelist = self.resolve_multinodes(user, path, directory, re_match)
    multiple = len(nodelist) > 1

    for node in nodelist:
      if multiple or 'Type' not in node or directory:
        self.printnode(node, details, bare, True)

    if not directory:
      for node in nodelist:
        if 'Type' in node:
          self.process_children(node, recurse, details, bare, False, multiple, self.printnode)

  def cd(self, path):
    user = self._smugmug.get_auth_user()
    matched_nodes, unmatched_dirs = self.path_to_node(user, path)

    newcd = matched_nodes[-1].path
    
    if unmatched_dirs:
      print('"%s" not found in "%s".' % (
        unmatched_dirs[0], newcd))
      return

    if 'FileName' in matched_nodes[-1].json:
      print(f'{newcd} is not a Folder or Album')
      return
    
    self._cwd = newcd
    print(self._cwd)

  def pwd(self):
    print(self._cwd)

  def make_node(self, user, paths, create_parents, node_type, privacy):
    user = user or self._smugmug.get_auth_user()
    for path in paths:
      matched_nodes, unmatched_dirs = self.path_to_node(user, path)
      if len(unmatched_dirs) > 1 and not create_parents:
        print('"%s" not found in "%s".' % (
          unmatched_dirs[0], matched_nodes[-1].path))
        continue

      if not len(unmatched_dirs):
        print('Path "%s" already exists.' % path)
        continue

      self._match_or_create_nodes(
        matched_nodes, unmatched_dirs, node_type, privacy)

  def _ask(self, question):
    answer = input(question)
    return answer.lower() in ['y', 'yes']

  def rmdir(self, user, remove_parents, recurse, force, dirs):
    user = user or self._smugmug.get_auth_user()
    for dir in dirs:
      matched_nodes, unmatched_dirs = self.path_to_node(user, dir)
      if unmatched_dirs:
        print('Folder or album "%s" not found.' % dir)
        continue

      matched_nodes.pop(0) # don't try to remove the root directory
      node = matched_nodes.pop()
      current_dir = node.path
      if 'Type' not in node:
        print(f'"{current_dir}" is not an album or directory')
        continue

      childcount = len(node.get_children({'count': 1}))
      if not recurse and childcount:
        print('Cannot delete %s: "%s" is not empty.' % (
          node['Type'], current_dir))
        break

      if not force:
        if not self._ask('Remove %s %s node "%s"? ' % ('empty' if childcount == 0 else 'non-empty', node['Type'], node.path)):
          continue;
      print('Removing "%s".' % current_dir)
      node.delete()

      if remove_parents:
        while matched_nodes:
          node = matched_nodes.pop()
          if len(node.get_children({'count': 1})) > 0:
            break
          print(f'Removing "{node.path}".')
          node.delete()

      node.parent.reset_cache()
      
  def rm(self, user, force, recursive, paths):
    user = user or self._smugmug.get_auth_user()
    for path in paths:
      nodelist = self.resolve_multinodes(user, path, True)
      for node in nodelist:
        nodetype = node['Type'] if 'Type' in node else 'File'

        if recursive or 'Type' not in node or len(node.get_children({'count': 1})) == 0:
          if force or self._ask('Remove %s node "%s"? ' % (nodetype, node.path)):
            print('Removing "%s".' % node.path)
            node.delete()
        else:
          print('%s "%s" is not empty.' % (nodetype, node.path))

  def upload(self, user, filenames, album):
    user = user or self._smugmug.get_auth_user()
    matched_nodes, unmatched_dirs = self.path_to_node(user, album)
    if unmatched_dirs:
      print('Album not found: "%s".' % album)
      return

    node = matched_nodes[-1]
    if 'Type' not in node or node['Type'] != 'Album':
      print(f'{matched_nodes[-1].name} is not an Album')
      return

    for filename in itertools.chain(*(glob.glob(f) for f in filenames)):
      file_basename = os.path.basename(filename).strip()
      if node.get_child(file_basename):
        print('Skipping "%s", file already exists in Album "%s".' % (filename,
                                                                     album))
        continue

      print('Uploading "%s" to "%s"...' % (filename, album))
      with open(filename, 'rb') as f:
        response = node.upload('Album',
                               file_basename,
                               f.read())
      if response.status_code != requests.codes.ok:
        print('Error uploading "%s" to "%s".' % (filename, album))
        print('Server responded with %s.' % str(response))
        return None

    node.reset_cache()

  def download(self, user, force, paths):
    user = user or self._smugmug.get_auth_user()

    for path in paths:
      nodelist = self.resolve_multinodes(user, path, True)
      for dlnode in nodelist:

        if 'FileName' not in dlnode:
          print(f'{dlnode.name} is not a downloadable file.')
          continue

        filename = dlnode['FileName']

        if os.path.exists(filename) and not force:
          print(f'{filename} already exists.')
          continue
      
        video = dlnode.json['IsVideo']
        locator = 'LargestVideo' if video else 'ImageDownload'
        downloaduri = dlnode.json['Uris'][locator]['Uri']
        result = self._smugmug.get_json(downloaduri)
        downloadurl = result['Response'][locator]['Url']
        size = result['Response']['LargestVideo']['Size'] if video else dlnode.json['ArchivedSize']
        
        print(f'Downloading {filename} ({size:,}) from {downloadurl}')
        self._smugmug.download(downloadurl, filename)

  def newdn(self, user, force, recurse, paths):
    user = user or self._smugmug.get_auth_user()

    albums = []
    files = []

    def process_node(node):
      if 'FileName' in node:
        files.append(node)
      elif node['Type'] == 'Folder':
        if recurse:
          for child in node.get_children():
            process_node(child)
      elif node['Type'] == 'Album':
        albums.append(node)
    
    for path in paths:
      nodelist = self.resolve_multinodes(user, path, True)
      for node in nodelist:
        process_node(node)

    print('Files to process:')
    for file in sorted(files):
      print(f'  {file.path}')
    
    print('Albums to process:')
    for album in sorted(albums):
      print(f'  {album.path}')

  def _get_common_path(self, matched_nodes, local_dirs):
    new_matched_nodes = []
    unmatched_dirs = list(local_dirs)
    for remote, local in zip(matched_nodes, unmatched_dirs):
      if local != remote.name:
        break
      new_matched_nodes.append(remote)
      unmatched_dirs.pop(0)
    return new_matched_nodes, unmatched_dirs

  def sync(self,
           user,
           sources,
           target,
           deprecated_target,
           force,
           privacy,
           folder_threads,
           file_threads,
           upload_threads,
           set_defaults):
    if set_defaults:
      self.smugmug.config['folder_threads'] = folder_threads
      self.smugmug.config['file_threads'] = file_threads
      self.smugmug.config['upload_threads'] = upload_threads
      print('Defaults updated.')
      return

    if deprecated_target:
      print('-t/--target argument no longer exists.')
      print('Specify the target folder as the last positional argument.')
      return

    # The argparse library doesn't seem to support having two positional
    # arguments, the first variable in length and the second optional.
    # The first positional argument always eagerly grabs all values specified.
    # We therefore need to distribute that last value to the second argument
    # when it's specified.
    if len(sources) >= 2 and target == [os.sep]:
      target = sources.pop()
    else:
      target = target[0]

    # Approximate worse case: each folder and file thread works on a different
    # folder, and all folders are 5 level deep.
    self._smugmug.garbage_collector.set_max_children_cache(
      folder_threads + file_threads + 5)

    # Make sure that the source paths exist.
    globbed = [(source, glob.glob(source)) for source in sources]
    print(f'globbed={globbed}')
    not_found = [g[0] for g in globbed if not g[1]]
    if not_found:
      print('File%s not found:\n  %s' % (
        's' if len(not_found) > 1 else '', '\n  '.join(not_found)))
      return
    all_sources = list(itertools.chain.from_iterable([g[1] for g in globbed]))

    file_sources = [s for s in all_sources if os.path.isfile(s)]
    dir_sources = [s for s in all_sources if os.path.isdir(s)]

    files_by_path = collections.defaultdict(list)
    for file_source in file_sources:
      path, filename = os.path.split(file_source)
      files_by_path[path or '.'].append(filename)

    # Make sure that the destination node exists.
    user = user or self._smugmug.get_auth_user()
    target = target if target.startswith(os.sep) else os.sep + target
    matched, unmatched_dirs = self.path_to_node(user, target)
    if unmatched_dirs:
      print('Target folder not found: "%s".' % target)
      return
    target_type = matched[-1]['Type'].lower()

    # Abort if invalid operations are requested.
    if target_type == 'folder' and file_sources:
      print('Can\'t upload files to folder. Please sync to an album node.')
      return
    elif (target_type == 'album' and
          any(not d.endswith(os.sep) for d in dir_sources)):
      print('Can\'t upload folders to an album. Please sync to a folder node.')
      return

    # Request confirmation before proceeding.
    if len(all_sources) == 1:
      print('Syncing "%s" to SmugMug %s "%s".' % (
        all_sources[0], target_type, target))
    else:
      print('Syncing:\n%s\nto SmugMug %s "%s".' % (
        '  ' + '\n  '.join(all_sources), target_type, target))
    if not force and not self._ask('Proceed (yes/no)? '):
      return

    with task_manager.TaskManager() as manager, \
         thread_safe_print.ThreadSafePrint(), \
         thread_pool.ThreadPool(upload_threads) as upload_pool, \
         thread_pool.ThreadPool(file_threads) as file_pool, \
         thread_pool.ThreadPool(folder_threads) as folder_pool:
      for source, walk_steps in sorted(
          [(d, os.walk(d)) for d in dir_sources] +
          [(p + os.sep, [(p, [], f)])
           for p, f in files_by_path.items()]):
        # Filter-out files and folders that must be ignored.
        steps = []
        for walk_step in walk_steps:
          if self._aborting:
            return
          subdir, dirs, files = walk_step
          configs = persistent_dict.PersistentDict(os.path.join(subdir,
                                                                '.smugcli'))
          ignored = set(configs.get('ignore', []))
          dirs[:] = set(dirs) - ignored  # Prune dirs from os.walk traversal.
          files[:] = set(files) - ignored
          steps.append((subdir, dirs, files))

        # Process files in sorted order to make unit tests deterministic. We
        # can't merge this loop with the previous one because calling `sorted`
        # directly on the result of os.walk in `walk_step` would prevent us from
        # pruning directories from the walk (os.walk returns a generator which
        # can't be iterated on multiple times).
        for walk_step in sorted(steps):
          if self._aborting:
            return
          folder_pool.add(self._sync_folder,
                          manager,
                          file_pool,
                          upload_pool,
                          source,
                          target,
                          privacy,
                          walk_step,
                          matched,
                          unmatched_dirs)
    print('Sync complete.')

  def _sync_folder(self,
                   manager,
                   file_pool,
                   upload_pool,
                   source,
                   target,
                   privacy,
                   walk_step,
                   matched,
                   unmatched_dirs):
    if self._aborting:
      return
    subdir, dirs, files = walk_step
    media_files = [f for f in files if self._is_media(f)]
    if media_files:
      rel_subdir = os.path.relpath(subdir, os.path.split(source)[0])
      target_dirs = os.path.normpath(
        os.path.join(target, rel_subdir)).split(os.sep)
      target_dirs = [d.strip() for d in target_dirs]

      if dirs:
        target_dirs.append('Images from folder ' + target_dirs[-1])

      matched, unmatched = self._get_common_path(matched, target_dirs)
      matched, unmatched = self._match_nodes(matched, unmatched)

      if unmatched:
        matched = self._match_or_create_nodes(
          matched, unmatched, 'Album', privacy)
      else:
        print('Found matching remote album "%s".' % os.path.join(*target_dirs))

      # Iterate in sorted order to make unit tests deterministic.
      for f in sorted(media_files):
        if self._aborting:
          return
        file_pool.add(self._sync_file,
                      manager,
                      os.path.join(subdir, f),
                      matched[-1],
                      upload_pool)

  def _sync_file(self, manager, file_path, node, upload_pool):
    if self._aborting:
      return
    with manager.start_task(1, '* Syncing file "%s"...' % file_path):
      file_name = file_path.split(os.sep)[-1].strip()
      with open(file_path, 'rb') as f:
        file_content = f.read()
      file_root, file_extension = os.path.splitext(file_name)
      if file_extension.lower() == '.heic':
        # SmugMug converts HEIC files to JPEG and renames them in the process
        renamed_file = file_root + '_' + file_extension[1:] + '.JPG'
        remote_file = node.get_child(renamed_file)
      else:
        remote_file = node.get_child(file_name)

      if remote_file:
        if remote_file['Format'].lower() in VIDEO_EXT:
          # Video files are modified by SmugMug server side, so we cannot use
          # the MD5 to check if the file needs a re-sync. Use the last
          # modification time instead.
          remote_time = datetime.datetime.strptime(
            remote_file.get('ImageMetadata')['DateTimeModified'],
            '%Y-%m-%dT%H:%M:%S')

          try:
            parser = guessParser(StringInputStream(file_content))
            metadata = extractMetadata(parser)
            file_time = max(metadata.getValues('last_modification') +
                            metadata.getValues('creation_date'))
          except Exception as err:
            print('Failed extracting metadata for file "%s".' % file_path)
            file_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))

          time_delta = abs(remote_time - file_time)
          same_file = (time_delta <= datetime.timedelta(seconds=1))
        elif file_extension.lower() == '.heic':
          # HEIC files are recoded to JPEG's server side by SmugMug so we cannot
          # use MD5 to check if file needs a re-sync. Moreover, no image
          # metadata (e.g. time taken timestamp) is kept in SmugMug that would
          # allow us to tell if the file is the same. Hence, for now we just
          # assume HEIC files never change and we never re-upload them.
          same_file = True
        else:
          remote_md5 = remote_file['ArchivedMD5']
          file_md5 = hashlib.md5(file_content).hexdigest()
          same_file = (remote_md5 == file_md5)

        if same_file:
          return  # File already exists on Smugmug

      if self._aborting:
        return
      upload_pool.add(self._upload_media,
                      manager,
                      node,
                      remote_file,
                      file_path,
                      file_name,
                      file_content)

  def _upload_media(self, manager, node, remote_file, file_path, file_name, file_content):
    if self._aborting:
      return
    if remote_file:
      print('File "%s" exists, but has changed. '
            'Deleting old version.' % file_path)
      remote_file.delete()
      task = '+ Re-uploading "%s"' % file_path
    else:
      task = '+ Uploading "%s"' % file_path

    def get_progress_fn(task):
      def progress_fn(percent):
        manager.update_progress(0, task, ': %d%%' % percent)
        return self._aborting
      return progress_fn

    with manager.start_task(0, task):
      node.upload('Album', file_name, file_content,
                  progress_fn=get_progress_fn(task))

    if remote_file:
      print('Re-uploaded "%s".' % file_path)
    else:
      print('Uploaded "%s".' % file_path)

  def _is_media(self, path):
    extension = os.path.splitext(path)[1][1:].lower().strip()
    return extension in self._media_ext
