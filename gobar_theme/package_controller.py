#! coding: utf-8
# pylint: disable-all
import cgi
from urllib import urlencode

import ckan.lib.helpers as h
import ckan.lib.navl.dictization_functions as dict_fns
import ckan.logic as logic
import ckan.model as model
import ckan.plugins as p
import ckanext.googleanalytics.plugin as google_analytics
import moment
from ckan.common import OrderedDict, _, request, c
from ckan.controllers.package \
    import PackageController, _encode_params, search_url, render, NotAuthorized, check_access, abort, get_action, log
from ckan.lib.search import SearchError
from paste.deploy.converters import asbool
from pylons import config
from webob.exc import status_map

CACHE_PARAMETERS = ['__cache', '__no_cache__']
NotFound = logic.NotFound
ValidationError = logic.ValidationError
exc = status_map[302]
tuplize_dict = logic.tuplize_dict
clean_dict = logic.clean_dict
parse_params = logic.parse_params


def collect_descendants(organization_list):
    partial = []
    for organization in organization_list:
        partial.append(organization['name'])
        if 'children' in organization and organization['children']:
            partial += collect_descendants(organization['children'])
    return partial


def search_organization(organization_name, organizations_branch=None):
    if organizations_branch is None:
        organizations_branch = logic.get_action('group_tree')({}, {'type': 'organization'})
    for organization in organizations_branch:
        if organization['name'] == organization_name:
            if 'children' in organization and organization['children']:
                return collect_descendants(organization['children'])
        elif 'children' in organization and organization['children']:
            inner_search = search_organization(organization_name, organization['children'])
            if inner_search:
                return inner_search
    return []


def custom_organization_filter(organization_name):
    descendant_organizations = search_organization(organization_name)
    if descendant_organizations and descendant_organizations:
        descendant_organizations_filter = ' OR '.join(descendant_organizations)
        organization_filter = '(%s OR %s)' % (organization_name, descendant_organizations_filter)
    else:
        organization_filter = organization_name
    return ' organization:%s' % organization_filter


class GobArPackageController(PackageController):

    def __generate_spatial_extra_field(self, data_dict):
        extras = data_dict['extras']

        def __find_extra(extras, key, create=False):
            for extra in extras:
                if extra['key'] == key:
                    return extra
            
            if create:
                extra = {'key': key}
                extras.append(extra)
                return extra

        country = __find_extra(extras, 'country')
        if country:
            spatial = __find_extra(extras, 'spatial', True)
            spatial['value'] = [country['value']]

            province = __find_extra(extras, 'province')
            if province and province['value']:
                spatial['value'].append(province['value'])

            district = __find_extra(extras, 'district')
            if district and district['value']:
                spatial['value'].append(district['value'])
            
            spatial['value'] = ','.join(spatial['value'])

    def search(self):
        package_type = self._guess_package_type()
        try:
            context = {'model': model, 'user': c.user or c.author, 'auth_user_obj': c.userobj}
            check_access('site_read', context)
        except NotAuthorized:
            abort(401, _('Not authorized to see this page'))
        q = c.q = request.params.get('q', u'')
        c.query_error = False
        page = h.get_page_number(request.params)
        limit = int(config.get('ckan.datasets_per_page', 20))
        params_nopage = [(k, v) for k, v in request.params.items() if k != 'page']

        def drill_down_url(alternative_url=None, **by):
            return h.add_url_param(
                alternative_url=alternative_url,
                controller='package',
                action='search',
                new_params=by
            )

        c.drill_down_url = drill_down_url

        def remove_field(key, value=None, replace=None):
            return h.remove_url_param(key, value=value, replace=replace, controller='package', action='search')

        c.remove_field = remove_field

        sort_by = request.params.get('sort', None)
        params_nosort = [(k, v) for k, v in params_nopage if k != 'sort']

        def _sort_by(fields):
            params = params_nosort[:]
            if fields:
                sort_string = ', '.join('%s %s' % f for f in fields)
                params.append(('sort', sort_string))
            return search_url(params, package_type)

        c.sort_by = _sort_by
        if not sort_by:
            c.sort_by_fields = []
        else:
            c.sort_by_fields = [field.split()[0] for field in sort_by.split(',')]

        def pager_url(q=None, page=None):
            params = list(params_nopage)
            params.append(('page', page))
            return search_url(params, package_type)

        c.search_url_params = urlencode(_encode_params(params_nopage))

        try:
            c.fields = []
            c.fields_grouped = {}
            search_extras = {}
            fq = ''
            for (param, value) in request.params.items():
                if param not in ['q', 'page', 'sort'] \
                        and value and not param.startswith('_'):
                    if not param.startswith('ext_'):
                        c.fields.append((param, value))
                        # Modificaci??n para andino: usamos una funci??n para buscar dependencias entre organizaciones
                        if param != 'organization':
                            fq += ' %s:"%s"' % (param, value)
                        else:
                            fq += custom_organization_filter(value)
                        if param not in c.fields_grouped:
                            c.fields_grouped[param] = [value]
                        else:
                            c.fields_grouped[param].append(value)
                    else:
                        search_extras[param] = value

            context = {'model': model, 'session': model.Session,
                       'user': c.user or c.author, 'for_view': True,
                       'auth_user_obj': c.userobj}

            if package_type and package_type != 'dataset':
                fq += ' +dataset_type:{type}'.format(type=package_type)
            elif not asbool(config.get('ckan.search.show_all_types', 'False')):
                fq += ' +dataset_type:dataset'

            facets = OrderedDict()

            default_facet_titles = {
                'organization': _('Organizations'),
                'groups': _('Groups'),
                'tags': _('Tags'),
                'res_format': _('Formats'),
                'license_id': _('Licenses'),
            }

            for facet in h.facets():
                if facet in default_facet_titles:
                    facets[facet] = default_facet_titles[facet]
                else:
                    facets[facet] = facet

            for plugin in p.PluginImplementations(p.IFacets):
                facets = plugin.dataset_facets(facets, package_type)

            c.facet_titles = facets

            data_dict = {
                'q': q,
                'fq': fq.strip(),
                'facet.field': facets.keys(),
                'rows': limit,
                'start': (page - 1) * limit,
                'sort': sort_by,
                'extras': search_extras
            }

            query = logic.action.get.package_search(context, data_dict)
            c.sort_by_selected = query['sort']

            c.page = h.Page(
                collection=query['results'],
                page=page,
                url=pager_url,
                item_count=query['count'],
                items_per_page=limit
            )
            c.search_facets = query['search_facets']
            c.page.items = query['results']
        except SearchError, se:
            log.error('Dataset search error: %r', se.args)
            c.query_error = True
            c.search_facets = {}
            c.page = h.Page(collection=[])
        c.search_facets_limits = {}
        for facet in c.search_facets.keys():
            try:
                # Modificaci??n para andino: chequeo si el facet es 'organization'
                if facet != 'organization':
                    limit = int(request.params.get('_%s_limit' % facet, int(config.get('search.facets.default', 10))))
                else:
                    limit = None
            except ValueError:
                error_description = _('Parameter "{parameter_name}" is not an integer')
                error_description = error_description.format(parameter_name='_%s_limit' % facet)
                abort(400, error_description)
            c.search_facets_limits[facet] = limit

        self._setup_template_variables(context, {}, package_type=package_type)

        return render(self._search_template(package_type), extra_vars={'dataset_type': package_type})

    def _save_new(self, context, package_type=None):
        # The staged add dataset used the new functionality when the dataset is
        # partially created so we need to know if we actually are updating or
        # this is a real new.
        is_an_update = False
        ckan_phase = request.params.get('_ckan_phase')
        from ckan.lib.search import SearchIndexError

        def pop_groups_from_data_dict_and_get_package_name_and_group_name(a_data_dict):
            # sacamos los grupos para que no fallen m??s adelante las validaciones de ckan
            some_group_names = [group['name'] for group in (a_data_dict['groups'] if 'groups' in a_data_dict else [])]
            a_data_dict['groups'] = []
            a_package_name = a_data_dict['name']  # El campo Name identifica un??vocamente a un Dataset
            return a_package_name, some_group_names

        def update_package_group_relation(a_package_name, group_names_to_add):
            # obtener id del package usando el a_package_name
            package = model.Package.get(a_package_name)

            # Es necesario eliminar *todos* los objetos `Member` que relacionan `Group`s con `Package`s
            # ya que vamos a reescribir esas relaciones seg??n el par??metro `group_names_to_add`
            for group in model.Session.query(model.Group):
                # con el ID del package queriear los Member con table_id = package_id eliminar
                members_to_delete = model.Session.query(model.Member).filter(
                    model.Member.group_id == group.id,
                    model.Member.table_name == 'package',
                    model.Member.table_id == package.id)
                for member in members_to_delete:
                    model.Session.delete(member)
            model.Session.commit()  # Hace falta el commit?

            # relaciono los datasets con los grupos correspondientes (que fueron ingresados)
            for group_name in group_names_to_add:
                group = model.Group.get(group_name)

                group.add_package_by_name(a_package_name)
                group.save()

        try:
            data_dict = clean_dict(dict_fns.unflatten(
                tuplize_dict(parse_params(request.POST))))

            # Guardamos como extras los campos issued y modified

            time_now = moment.now().isoformat()

            if 'extras' not in data_dict.keys():
                data_dict['extras'] = []
            self._add_or_replace_extra(key='issued', value=time_now, extras=data_dict['extras'])
            self._add_or_replace_extra(key='modified', value=time_now, extras=data_dict['extras'])
            superTheme = []
            for field in data_dict['extras']:
                if (field['key'] == 'superTheme' or field['key'] == 'globalGroups') and field['value'] != []:
                    superTheme = field['value']
                    break
            self._add_or_replace_extra(key='superTheme', value=superTheme, extras=data_dict['extras'])

            if ckan_phase:
                # prevent clearing of groups etc
                context['allow_partial_update'] = True
                # sort the tags
                if 'tag_string' in data_dict:
                    data_dict['tags'] = self._tag_string_to_list(data_dict['tag_string'])

                self._validate_dataset(data_dict)

                # Limpiamos el data_dict para poder guardar el DS aun siendo colaborador no miembro del grupo
                package_name, group_names = pop_groups_from_data_dict_and_get_package_name_and_group_name(data_dict)

                if data_dict.get('pkg_name'):
                    is_an_update = True
                    # This is actually an update not a save
                    data_dict['id'] = data_dict['pkg_name']
                    del data_dict['pkg_name']
                    # don't change the dataset state
                    if data_dict.get('save', '') == u'go-metadata':
                        data_dict['state'] = 'active'
                    else:
                        data_dict['state'] = 'draft'
                    # this is actually an edit not a save
                    pkg_dict = get_action('package_update')(context, data_dict)

                    # Restauramos los grupos asignados al dataset (cuando es un update)
                    update_package_group_relation(package_name, group_names)

                    if request.params['save'] == 'go-metadata':
                        # redirect to add metadata
                        url = h.url_for(controller='package', action='new_metadata', id=pkg_dict['name'])
                    elif request.params['save'] == 'save-draft':
                        url = h.url_for(controller='package', action='read', id=pkg_dict['name'])
                    else:
                        # redirect to add dataset resources
                        url = h.url_for(controller='package', action='new_resource', id=pkg_dict['name'])
                    raise exc(location=url).exception
                # Make sure we don't index this dataset
                if request.params['save'] not in ['go-resource', 'go-metadata']:
                    data_dict['state'] = 'draft'
                # allow the state to be changed
                context['allow_state_change'] = True

            data_dict['type'] = package_type
            context['message'] = data_dict.get('log_message', '')

            self.__generate_spatial_extra_field(data_dict)

            pkg_dict = get_action('package_create')(context, data_dict)

            # Restauramos los grupos asignados al dataset (cuando es un insert)
            update_package_group_relation(package_name, group_names)

            if ckan_phase and request.params['save'] != 'save-draft':
                url = h.url_for(controller='package', action='new_resource', id=pkg_dict['name'])
                raise exc(location=url).exception
            elif request.params['save'] == 'save-draft':
                url = h.url_for(controller='package', action='read', id=pkg_dict['name'])
                raise exc(location=url).exception
            self._form_save_redirect(pkg_dict['name'], 'new', package_type=package_type)
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound, e:
            abort(404, _('Dataset not found'))
        except dict_fns.DataError:
            abort(400, _(u'Integrity Error'))
        except SearchIndexError, e:
            try:
                exc_str = unicode(repr(e.args))
            except Exception:  # We don't like bare excepts
                exc_str = unicode(str(e))
            abort(500, _(u'Unable to add package to search index.') + exc_str)
        except ValidationError, e:
            errors = e.error_dict
            error_summary = e.error_summary
            if is_an_update:
                # we need to get the state of the dataset to show the stage we
                # are on.
                pkg_dict = get_action('package_show')(context, data_dict)
                data_dict['state'] = pkg_dict['state']
                return self.edit(data_dict['id'], data_dict,
                                 errors, error_summary)
            data_dict['state'] = 'none'
            return self.new(data_dict, errors, error_summary)

    def _save_edit(self, name_or_id, context, package_type=None):
        from ckan.lib.search import SearchIndexError
        log.debug('Package save request name: %s POST: %r',
                  name_or_id, request.POST)
        try:
            data_dict = clean_dict(dict_fns.unflatten(
                tuplize_dict(parse_params(request.POST))))

            self._validate_dataset(data_dict)

            if '_ckan_phase' in data_dict:
                # we allow partial updates to not destroy existing resources
                context['allow_partial_update'] = True
                if 'tag_string' in data_dict:
                    data_dict['tags'] = self._tag_string_to_list(
                        data_dict['tag_string'])
                del data_dict['_ckan_phase']
                del data_dict['save']
            context['message'] = data_dict.get('log_message', '')
            data_dict['id'] = name_or_id

            # Obtengo la lista de extras del dataset y agrego sus extras faltantes en el formulario
            extra_fields = get_action('package_show')(dict(context, for_view=True), {'id': name_or_id})['extras']
            if 'extras' not in data_dict.keys():
                data_dict['extras'] = []
            form_extras_keys = [x['key'] for x in data_dict['extras']]
            for extra_field in extra_fields:
                if extra_field.get('key') not in form_extras_keys:
                    data_dict['extras'].append(extra_field)
                    form_extras_keys.append(extra_field.get('key'))

            time_now = moment.now().isoformat()

            self._add_or_replace_extra(key='modified', value=time_now, extras=data_dict['extras'])

            self.__generate_spatial_extra_field(data_dict)

            pkg = get_action('package_update')(context, data_dict)
            c.pkg = context['package']
            c.pkg_dict = pkg

            self._form_save_redirect(pkg['name'], 'edit',
                                     package_type=package_type)
        except NotAuthorized:
            abort(403, _('Unauthorized to read package %s') % id)
        except NotFound, e:
            abort(404, _('Dataset not found'))
        except dict_fns.DataError:
            abort(400, _(u'Integrity Error'))
        except SearchIndexError, e:
            try:
                exc_str = unicode(repr(e.args))
            except Exception:  # We don't like bare excepts
                exc_str = unicode(str(e))
            abort(500, _(u'Unable to update search index.') + exc_str)
        except ValidationError, e:
            errors = e.error_dict
            error_summary = e.error_summary

        return self.edit(name_or_id, data_dict, errors, error_summary)

    def new_resource(self, id, data=None, errors=None, error_summary=None):
        ''' FIXME: This is a temporary action to allow styling of the
        forms. '''
        if request.method == 'POST' and not data:
            save_action = request.params.get('save')
            data = data or clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(request.POST))))
            # we don't want to include save as it is part of the form
            del data['save']
            resource_id = data['id']
            del data['id']

            # Guardo los campos issued y modified
            time_now = moment.now().isoformat()
            data['issued'] = time_now
            data['modified'] = time_now

            self._validate_resource(data)

            context = {'model': model, 'session': model.Session,
                       'user': c.user, 'auth_user_obj': c.userobj}

            if save_action == 'go-dataset':
                # go to first stage of add dataset
                h.redirect_to(controller='package', action='edit', id=id)

            # see if we have any data that we are trying to save
            data_provided = False
            for key, value in data.iteritems():
                if ((value or isinstance(value, cgi.FieldStorage))
                        and key not in ['resource_type', 'license_id', 'attributesDescription']):
                    data_provided = True
                    break

            if not data_provided and save_action != "go-dataset-complete":
                if save_action == 'go-dataset':
                    # go to first stage of add dataset
                    h.redirect_to(controller='package', action='edit', id=id)
                try:
                    data_dict = get_action('package_show')(context, {'id': id})
                except NotAuthorized:
                    abort(403, _('Unauthorized to update dataset'))
                except NotFound:
                    abort(404, _('The dataset {id} could not be found.'
                                 ).format(id=id))
                if not len(data_dict['resources']):
                    # no data so keep on page
                    msg = _('You must add at least one data resource')
                    # On new templates do not use flash message

                    if asbool(config.get('ckan.legacy_templates')):
                        h.flash_error(msg)
                        h.redirect_to(controller='package',
                                      action='new_resource', id=id)
                    else:
                        errors = {}
                        error_summary = {_('Error'): msg}
                        return self.new_resource(id, data, errors,
                                                 error_summary)
                # XXX race condition if another user edits/deletes
                data_dict = get_action('package_show')(context, {'id': id})
                get_action('package_update')(
                    dict(context, allow_state_change=True),
                    dict(data_dict, state='active'))
                h.redirect_to(controller='package', action='read', id=id)

            data['package_id'] = id
            try:
                if resource_id:
                    data['id'] = resource_id
                    get_action('resource_update')(context, data)
                else:
                    get_action('resource_create')(context, data)
            except ValidationError, e:
                errors = e.error_dict
                error_summary = e.error_summary
                return self.new_resource(id, data, errors, error_summary)
            except NotAuthorized:
                abort(403, _('Unauthorized to create a resource'))
            except NotFound:
                abort(404, _('The dataset {id} could not be found.'
                             ).format(id=id))
            if save_action == 'go-metadata':
                # XXX race condition if another user edits/deletes
                data_dict = get_action('package_show')(context, {'id': id})
                get_action('package_update')(
                    dict(context, allow_state_change=True),
                    dict(data_dict, state='active'))
                h.redirect_to(controller='package', action='read', id=id)
            elif save_action == 'go-dataset-complete':
                # go to first stage of add dataset
                h.redirect_to(controller='package', action='read', id=id)
            elif save_action == 'save-draft':
                h.redirect_to(controller='package', action='read', id=id)
            else:
                # add more resources
                h.redirect_to(controller='package', action='new_resource',
                              id=id)

        # get resources for sidebar
        context = {'model': model, 'session': model.Session,
                   'user': c.user, 'auth_user_obj': c.userobj}
        try:
            pkg_dict = get_action('package_show')(context, {'id': id})
        except NotFound:
            abort(404, _('The dataset {id} could not be found.').format(id=id))
        try:
            check_access(
                'resource_create', context, {"package_id": pkg_dict["id"]})
        except NotAuthorized:
            abort(403, _('Unauthorized to create a resource for this package'))

        package_type = pkg_dict['type'] or 'dataset'

        errors = errors or {}
        error_summary = error_summary or {}
        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary, 'action': 'new',
                'resource_form_snippet': self._resource_form(package_type),
                'dataset_type': package_type}
        vars['pkg_name'] = id
        # required for nav menu
        vars['pkg_dict'] = pkg_dict
        template = 'package/new_resource_not_draft.html'
        if pkg_dict['state'].startswith('draft'):
            vars['stage'] = ['complete', 'active']
            template = 'package/new_resource.html'
        return render(template, extra_vars=vars)

    def resource_edit(self, id, resource_id, data=None, errors=None,
                      error_summary=None):
        context = {'model': model, 'session': model.Session,
                   'api_version': 3, 'for_edit': True,
                   'user': c.user, 'auth_user_obj': c.userobj}
        data_dict = {'id': id}

        try:
            check_access('package_update', context, data_dict)
        except NotAuthorized:
            abort(403, _('User %r not authorized to edit %s') % (c.user, id))

        if request.method == 'POST' and not data:
            data = data or \
                clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
                                                           request.POST))))
            # we don't want to include save as it is part of the form
            del data['save']

            # Guardo los campos issued y modified
            package_data = get_action('resource_show')(context, {'id': resource_id})
            # Los packages creados sin el campo extra "issued" deben defaultear al campo "created"
            issued = package_data.get('issued', None) or package_data.get('created')
            data['issued'] = issued
            data['modified'] = moment.now().isoformat()

            data['package_id'] = id
            try:
                if resource_id:
                    data['id'] = resource_id
                    get_action('resource_update')(context, data)
                else:
                    get_action('resource_create')(context, data)
            except ValidationError, e:
                errors = e.error_dict
                error_summary = e.error_summary
                return self.resource_edit(id, resource_id, data,
                                          errors, error_summary)
            except NotAuthorized:
                abort(401, _('Unauthorized to edit this resource'))
            raise exc(location=h.url_for(
                controller='package', action='resource_read', id=id, resource_id=resource_id)).exception

        pkg_dict = get_action('package_show')(context, {'id': id})
        if pkg_dict['state'].startswith('draft'):
            # dataset has not yet been fully created
            resource_dict = get_action('resource_show')(context,
                                                        {'id': resource_id})
            return self.new_resource(id, data=resource_dict)
        # resource is fully created
        try:
            resource_dict = get_action('resource_show')(context,
                                                        {'id': resource_id})
        except NotFound:
            abort(404, _('Resource not found'))
        c.pkg_dict = pkg_dict
        c.resource = resource_dict
        # set the form action
        c.form_action = h.url_for(controller='package',
                                  action='resource_edit',
                                  resource_id=resource_id,
                                  id=id)
        if not data:
            data = resource_dict

        package_type = pkg_dict['type'] or 'dataset'

        errors = errors or {}
        error_summary = error_summary or {}
        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary, 'action': 'edit',
                'resource_form_snippet': self._resource_form(package_type),
                'dataset_type': package_type}
        return render('package/resource_edit.html', extra_vars=vars)

    def _validate_length(self, data, attribute, max_length):
        if len(data[attribute]) > max_length:
            raise ValidationError("%s must not have more than %s characters." % (attribute, max_length))

    def _validate_resource(self, data_dict):
        max_name_characters = 150
        max_desc_characters = 200
        self._validate_length(data_dict, 'name', max_name_characters)
        self._validate_length(data_dict, 'description', max_desc_characters)

    def _validate_dataset(self, data_dict):
        max_title_characters = 100
        max_desc_characters = 500
        self._validate_length(data_dict, 'title', max_title_characters)
        self._validate_length(data_dict, 'notes', max_desc_characters)

    def resource_view_embed(self, resource_id):
        google_analytics._post_analytics(c.user, 'CKAN Resource Embed', 'Resource ', resource_id, resource_id)

    def _add_or_replace_extra(self, key, value, extras):
        extra_field = list(filter(lambda x: x['key'] == key, extras))
        if extra_field:
            # Asumimos que hay un solo resultado
            extra_field[0]['value'] = value
        else:
            extras.append({'key': key, 'value': value})
