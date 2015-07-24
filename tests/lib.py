from __future__ import absolute_import
from cStringIO import StringIO
import json
import sys
import unittest

from django.db import models
from django.core import exceptions
from django.test import TestCase, TransactionTestCase, testcases
from django.contrib.auth.models import User

from tagulous import constants as tag_constants
from tagulous import models as tag_models
from tagulous import forms as tag_forms
from tagulous import admin as tag_admin
from tagulous import utils as tag_utils
from tagulous import settings as tag_settings

from tests.tagulous_tests_app import models as test_models
from tests.tagulous_tests_app import admin as test_admin
from tests.tagulous_tests_app import urls as test_urls
from tests.tagulous_tests_app import forms as test_forms
from tests.tagulous_tests_app2 import models as test_models2


class TagTestManager(object):
    """
    Test mixin to help test tag models
    """
    # Add test app urls
    urls = 'tests.tagulous_tests_app.urls'
    
    # We have some very long string comparisons (eg form field renders,
    # migrations), so set the maxDiff to 10k
    maxDiff = 1024 * 20
    
    # This class can manage models
    manage_models = None
    
    # Show normal fail messages in addition to any custom
    longMessage = True
    
    def setUp(self):
        """
        Ensure initial data is in the tag models
        """
        if self.manage_models is not None:
            for model in self.manage_models:
                tag_models.initial.model_initialise_tags(model)
                tag_models.initial.model_initialise_tags(model)
        
        if hasattr(self, 'setUpExtra'):
            self.setUpExtra()
    
    def tearDown(self):
        """
        Common tear down operations
        """
        # Only here for consistency
        if hasattr(self, 'tearDownExtra'):
            self.tearDownExtra()
        
    def create(self, model, **kwargs):
        ##30# ++ This can be replaced when we've got create() working properly
        normal = {}
        tagfield = {}
        mmfield = {}
        for field_name, val in kwargs.items():
            if isinstance(
                model._meta.get_field(field_name), tag_models.TagField
            ):
                tagfield[field_name] = val
            elif isinstance(
                model._meta.get_field(field_name), models.ManyToManyField
            ):
                mmfield[field_name] = val
            else:
                normal[field_name] = val
        
        # Create as normal
        item = model.objects.create(**normal)
        
        # Add tagfields (may not be using enhanced queryset)
        for field_name, val in tagfield.items():
            setattr(item, field_name, val)
            getattr(item, field_name).save()
        
        # Add mm fields
        for field_name, val in mmfield.items():
            field = getattr(item, field_name)
            for obj in val:
                field.add(obj)
        
        return item
    
    def assertInstanceEqual(self, instance, **kwargs):
        # First, reload instance
        instance = instance.__class__.objects.get(pk=instance.pk)
        
        # Check values
        for field_name, val in kwargs.items():
            try:
                if isinstance(
                    instance.__class__._meta.get_field(field_name),
                    (tag_models.SingleTagField, tag_models.TagField)
                ) and isinstance(val, basestring):
                    self.assertEqual(str(getattr(instance, field_name)), val)
                elif isinstance(
                    instance.__class__._meta.get_field(field_name),
                    models.ManyToManyField
                ):
                    mm_objs = list(getattr(instance, field_name).all())
                    self.assertEqual(len(val), len(mm_objs))
                    for obj in val:
                        self.assertTrue(obj in mm_objs)
                else:
                    self.assertEqual(getattr(instance, field_name), val)
            except AssertionError, e:
                self.fail(
                    'Instances not equal for field %s: %s' % (field_name, e)
                )

    def assertTagModel(self, model, tag_counts):
        """
        Assert the tag model matches the specified tag counts
        """
        if isinstance(model, (tag_models.SingleTagDescriptor, tag_models.TagDescriptor)):
            model = model.tag_model
        
        if len(tag_counts) != model.objects.count():
            self.fail("Incorrect number of tags in '%s'; expected %d, got %d" % (model, len(tag_counts), model.objects.count()))
        
        for tag_name, count in tag_counts.items():
            try:
                tag = model.objects.get(name=tag_name)
            except model.DoesNotExist:
                self.fail("Tag model missing expected tag '%s'" % tag_name)
            if tag.count != count:
                self.fail("Tag count for '%s' incorrect; expected %d, got %d" % (tag_name, count, tag.count))
    
    def _extract_json(self, dom, path=''):
        "Recursively break out json from a django.utils.html_parser dom object"
        jsons = {}
        if isinstance(dom, basestring):
            return dom, {}
        
        el_name = path + '.' + dom.name
        
        # Extract json from this element
        for i, attr in enumerate(dom.attributes):
            attr_name, attr_val = attr
            if (
                attr_name.startswith('data-')
                and attr_val[0] == '{'
                and attr_val[-1] == '}'
            ):
                if el_name not in jsons:
                    jsons[el_name] = {}
                jsons[el_name][attr_name] = attr_val
                dom.attributes[i] = (attr_name, '{/*json*/}')
        
        # Look at children
        for child in dom.children:
            new_child, new_jsons = self._extract_json(child, el_name)
            jsons.update(new_jsons)
        return dom, jsons
        
    def assertHTMLEqual(self, html1, html2, msg=None):
        """
        Clone of django's method, but with support for JSON in data- tag
        attributes
        """
        dom1 = testcases.assert_and_parse_html(
            self, html1, msg,
            'First argument is not valid HTML:'
        )
        dom2 = testcases.assert_and_parse_html(
            self, html2, msg,
            'Second argument is not valid HTML:'
        )
        
        # Walk the trees and pull out json
        dom1, json1 = self._extract_json(dom1)
        dom2, json2 = self._extract_json(dom2)
        
        # Convert dom back to string, call super to test doms
        # Yes it's inefficient, but it's tests and saves me from forking it
        super(TagTestManager, self).assertHTMLEqual(str(dom1), str(dom2))
        
        # Test jsons
        # Assert we've found the same elements
        self.assertItemsEqual(json1.keys(), json2.keys())
        for dom_path in json1.keys():
            # Assert the element has the same attributes
            self.assertItemsEqual(
                json1[dom_path].keys(), json2[dom_path].keys(),
                msg='%s has attributes do not match: %r != %r' % (
                    dom_path, json1[dom_path].keys(), json2[dom_path].keys(),
                )
            )
            for attr_name in json1[dom_path].keys():
                # ++ Can use assertJSONEqual after dropping 1.4 support
                # For now, just have a copy of it
                try:
                    json1_val = json.loads(json1[dom_path][attr_name])
                except ValueError:
                    self.fail(
                        "%s %s test result is not valid JSON: %r" %
                        (dom_path, attr_name, json1[dom_path][attr_name])
                    )
                else:
                    try:
                        json2_val = json.loads(json2[dom_path][attr_name])
                    except ValueError:
                        self.fail(
                            "%s %s expected is not valid JSON: %r" %
                            (dom_path, attr_name, json2[dom_path][attr_name])
                        )
                    else:
                        self.assertEqual(
                            json1_val, json2_val,
                            msg='%s %s JSON does not match' % (dom_path, attr_name)
                        )
    
    def debugTagModel(self, model):
        """
        Print tag model tags and their counts, to help debug failed tests
        """
        print "-=-=-=-=-=-"
        if isinstance(model, (tag_models.SingleTagDescriptor, tag_models.TagDescriptor)):
            model = model.tag_model
        print "Tag model: %s" % model
        for tag in model.objects.all():
            print '%s: %d' % (tag.name, tag.count)
        print "-=-=-=-=-=-"
    
    def prettyPrint(self, data):
        "Pretty print data, to help debug failed tests"
        import pprint
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(data)


# Based on http://stackoverflow.com/questions/16571150/how-to-capture-stdout-output-from-a-python-function-call
class Capturing(list):
    "Capture stdout and stderr to a string"
    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = sys.stderr = self._stringio = StringIO()
        return self
    def __exit__(self, *args):
        self.extend(self._stringio.getvalue().splitlines())
        sys.stdout = self._stdout
        sys.stderr = self._stderr
