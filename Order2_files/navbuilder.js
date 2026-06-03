$(function () {
    var linkspercol = 8;
    var menuObjects = [];
    //var navTitles = [];
    var navList = "";
    var navElement = $('.nav-list');
    //var menuHTML = [];
    var navArray = [];
    var imagesFolder = "https://tcgplayer-cdn.tcgplayer.com/static/nav";
    var allGames = {};
    //var htmlMenus = [];

    $.get(SITEROOT+'home/getnavxml', function(xml) {
        // this function pulls the xml and iterates through it, forming an object
        // which contains the whole menu structure.  once formed, that object can
        // be run through separate functions to construct the menus and nav.
        var menuItems = $(xml).find('menu-item');
        navArray = [];
        //menuItem = {}
        //iterate through menu items
        menuItems.each(function() {
            var xo = {};
            xo.boxstyle = (Boolean($(this).attr('box-style')) || false); //is this going to display in box style like 'all games'
            xo.title = $(this).children('nav-title').text(); //full name
            xo.menuTitle = $(this).children('menu-title').text(); //menu friendly name
            xo.guide = $(this).children('guide-url').text(); //price guide url
            navList += "<li><div class=\"nav-arrow-shell\"><div class=\"nav-arrow-tip\"></div></div>" + xo.menuTitle + "</li>";
            navArray.push(xo.menuTitle);
            xo.url = $(this).children('url').text();
            xo.sections = [];
            //iterate through sections for menu item (ie cards, events)
            $(this).children('links').children('section').each(function() {
                var xt = {};
                xt.title = $(this).attr('title');
                xt.blue = (Boolean($(this).attr('blue-links')) || false); //are these links to be treated as 'blue links' in the css
                //xt.format = (Boolean($(this).attr('format')) || false);
                xt.links = [];
                //iterate through each of the links within a section
                $(this).each(function() {
                    $(this).children('link').each(function () {
                        var xl = {};
                        xl.header = (Boolean($(this).attr('header')) || false);
                        var lc = $(this);
                        xl.title = (lc.children('display-name').text());
                        xl.image = (lc.children('image').text()) || ""; //this is the image that appears in the left column in card-based menus
                        xl.url = (lc.children('url').text());
                        xl.sublinks = [];
                        //iterate through each link items' sub-links
                        lc.children('sub-link').each(function() { //form sublinks under main link if they exist
                            var xs = {};
                            xs.title = $(this).attr('title');
                            xs.url = $(this).text();
                            xl.sublinks.push(xs);
                        });
                        xt.links.push(xl);
                    });
                    xo.sections.push(xt);
                });
            });
            menuObjects.push(xo);
        });

        //
        navElement.html(navList);
        //if you need to manually plop links on the end of the nav, do it here and add the class .link to your <li>
        navElement.append("<li class=\"link\" style=\"color:#337abc; float:right;\"><a href=\"http://prices.tcgplayer.com/price-guide\">PRICE GUIDE</a></li>");

        $(function () {
            fillNav(); // construct the nav
            buildMenus(menuObjects, 0); //construct the menus from the menu object
        });

    }); // end .get

    function fillNav() {

        $('.nav-list li:not(.link)').click(function () {
            var li = $(this);

            //Hide if already shown
            if ($(li).hasClass('nav-list-over')) {
                $('.nav-arrow-shell').css('display', 'none');
                $('.nav-list').children().removeClass("nav-list-over green-fade post");
                $('.nav-shell').removeClass("nav-underline");
                $('.menu-panel-main').fadeOut(80);
                event.stopPropagation();
            }
            else if (!$(li).hasClass('nav-list-over')) { //show if hidden
                $('.nav-list').children().removeClass("nav-list-over green-fade post");
                $('.nav-arrow-shell').css('display', 'none');
                $(li).addClass("nav-list-over green-fade");
                $(li).children('.nav-arrow-shell').fadeIn(300);
                $(li).toggleClass('post');
                $('.nav-shell').addClass("nav-underline");
                var i = $(li).index();
                $('.menu-panel-content').fadeOut(120, function () {
                    buildMenus(menuObjects, i);
                    $('.menu-panel-content').fadeIn(100);
                });
                $('.menu-panel-main').fadeIn(190);
                event.stopPropagation();
            }
        });

        //Open new tabs when you mouse over once the nav is expanded
        $('.nav-list li:not(.link)').mouseover(function() {
            var li = $(this);
            if ($('.menu-panel-main').css('display') != 'none' && !$(li).hasClass('nav-list-over')) {
                $('.nav-list').children().removeClass("nav-list-over green-fade post");
                $('.nav-arrow-shell').css('display', 'none');
                $(li).addClass("nav-list-over green-fade");
                $(li).children('.nav-arrow-shell').fadeIn(300);
                $(li).toggleClass('post');
                $('.nav-shell').addClass("nav-underline");
                var i = $(li).index();
                $('.menu-panel-content').fadeOut(120, function () {
                    buildMenus(menuObjects, i);
                    $('.menu-panel-content').fadeIn(100);
                });
                $('.menu-panel-main').fadeIn(190);
            }
        });

        $('.nav-list .link').mouseover(function () {
            //do nothing woooo
        });

        //Close the nav when you mouse out
        $('.nav-container').mouseleave(function () {
            if ($('.nav-list li').hover()) {
                $('.nav-arrow-shell').css('display', 'none');
                $('.nav-list').children().removeClass("nav-list-over green-fade post");
                $('.nav-shell').removeClass("nav-underline");
                $('.menu-panel-main').fadeOut(80);
            }
        });

       // $('#menu-fb').click(function () {
       //     window.open("https://www.facebook.com/tcgplayer");
       // });
       // $('#menu-twitter').click(function () {
       //     window.open("https://twitter.com/TCGplayer");
       // });
    };

    function fillGamesPanel(allGames) {//build the special all games panel
        var ll = allGames.sections[0].links.length; //number of games in All Games xml
        var gamesHtml = "<div class=\"clear-header\"><span class=\"clear-header-text\">"+allGames.title+"</span></div>";
        var sb = -1;
        var tcg = -1;
        for (var a = 0; a < ll; a++) {
            var p = allGames.sections[0].links[a];
            var i = "<div class=\"imageholder\" id=\"allgames_" + a + "\"><img src=\"" + imagesFolder + "/" + p.image + "\" alt=\"Products in " + p.title + "\"/></div>";
            gamesHtml += "<div class=\"menu-game-square\">" + i + p.title + "<div class=\"menu-hover-panel\"></div></div>";
            if (p.title == "Supplies") {
                sb = a;
            }
            if (p.title.substr(0, 3) == "TCG") {
                tcg = a;
            }
        }
        
        $("#menuContent").html(gamesHtml);
        if (sb > -1) {
            suppliesBox(sb);
            sb = -1;
        }
        
        if (tcg > -1) {
            tcgBox(tcg);
            tcg = -1;
        }
        $(".menu-game-square").mouseover(function () {
            $(this).children('.menu-hover-panel').fadeIn(150);
        }).mouseleave(function () {
            $(this).children('.menu-hover-panel').fadeOut(150);
        }).click(function (index) {
            var w = $(this).index();
            window.location.href = fixURL(allGames.sections[0].links[w - 1].url);
        })
    }

    function buildMenus(navObj, t) {
        var thisItem = {};
        var blueItems = [];
        var tempBuild = "";
        var sidebarBuild = "";
        var selectBuildColumn = new Array(tempBuild, sidebarBuild);
        var o = $(navObj[t])[0];
        //$(navObj).each(function () {
            //var o = $(this)[0];
        var currentSection = 0;
        linksmod = 0;
        allGames = {}; 
        $(o).each(function () {
            var p = $(this);
            //game titles
            p.each(function () {
                if ($(this)[0] && !$(this)[0].boxstyle) {//dont do all the usual stuff if this is box style
                    if ($(this)[0].menuTitle == "Supplies") {
                        linksmod = 6;
                    } else {
                        linksmod = 0;
                    }
                    var q = $(this.sections);
                    $(q).each(function (index) {//iterate through nav items
                        currentSection = (index > 0) ? 1 : 0; //is this the first section or some later section?
                        thisItem.menuTitle = $(this)[0].menuTitle;
                        thisItem.title = $(this)[0].title;
                        var ccount = 0;
                        var blueLink = false;
                        $(this).each(function (index) { //iterate through section contents
                            blueLink = $(this)[0].blue;
                            if (currentSection == 0) { //if this is the 'cards' or first section
                                if (blueLink) {
                                    //
                                } else {
                                    selectBuildColumn[currentSection] += "<div class=\"menu-five-column\" id=\"mcol" + index + "\">";
                                }
                                selectBuildColumn[currentSection] += "<div class=\"menu-column-content card-column\">";
                                if (blueLink) {
                                    selectBuildColumn[currentSection] += "<div class=\"menu-links\">";
                                } else {
                                }
                            } else {
                                if ($(this)[0].title !== "") {
                                    selectBuildColumn[currentSection] += "<div class=\"clear-header\"><span class=\"clear-header-text\">" + $(this)[0].title + "</span></div>";
                                }
                                if (blueLink) {
                                    selectBuildColumn[currentSection] += "<div class=\"menu-blue-header\"></div>"
                                }
                            }

                            // add section title to HTML
                            var lc = $(this.links).length;
                            $(this.links).each(function (index) {
                                if (index < (2 * (linkspercol+linksmod))) {
                                    // individual links within section
                                    var u = $(this)[0].url;
                                    var tt;
                                    if (blueLink) {
                                        tt = $(this)[0].title;
                                        selectBuildColumn[currentSection] += "<p class=\"menu-view-all\"><a href=\"" + fixURL(u) + "\">" + tt + "</a></p>"; //add link title, main URL to HTML
                                    } else {
                                        tt = $(this)[0].title.substring(0, 27);
                                        var hv = "";
                                        var hx="";  
                                        if ($(this)[0].header) {
                                            hv = "<span class=\"menu-item-header\">";
                                            hx = "</span>";
                                        }
                                        selectBuildColumn[currentSection] += "<p>";
                                        if ($(this)[0].sublinks.length > 0) { };//tempBuild+= "<i class=\"drop fa fa-chevron-right\"></i> ";}

                                        selectBuildColumn[currentSection] += "<a href=\"" + fixURL(u) + "\">" + hv + tt + hx + "</a>";
                                        selectBuildColumn[currentSection] += "</p>";

                                    }

                                    selectBuildColumn[currentSection] += "<div class=\"menu-sublinks\">";
                                    var pt = "";
                                    $($(this)[0].sublinks).each(function () {  //iterate through sublinks


                                        selectBuildColumn[currentSection] += "<span class=\"menu-column-small-text\"><a href=\"" + fixURL($(this)[0].url) + "\">" + $(
                                            this)[0].title + "</a></span>";
                                        pt += "<a href=\"" + fixURL($(this)[0].url) + "\">" + $(
                                            this)[0].title + "</a>&nbsp;&nbsp;|&nbsp;&nbsp;";
                                    });
                                    pt = pt.substring(0, pt.length - 13);
                                    $(this)[0].subcomp = pt;
                                    selectBuildColumn[currentSection] += "</div>";
                                    ccount++;
                                    if (currentSection == 0) {
                                        if (ccount == (linkspercol+linksmod) && index + 1 !== lc) {

                                            selectBuildColumn[currentSection] += "</div></div>";
                                            selectBuildColumn[currentSection] += "<div class=\"menu-five-column menu-column-short-bg\"><div class=\"menu-column-content card-column\">";

                                            ccount = 0;
                                        } else if (ccount == (linkspercol+linksmod) && index + 1 == lc) {
                                            selectBuildColumn[currentSection] += "</div></div>";
                                        }
                                    }
                                }
                            });
                            if (currentSection == 0) {
                                selectBuildColumn[currentSection] += "</div></div>";
                            }
                        }); // end section links each()
                    });
                    // end column 
                } else { //else if this nav item IS a "box" formatted menu
                    allGames = $(this)[0];
                    fillGamesPanel(allGames); // build the boxy panel
                }
            }); // end sections (p) each()
        //});
        });
        //$("#mcol0").addClass("menu-column-no-bg"); //keep bg stripe off of last column
        /////////
        if ($(navObj[t])[0] && !$(navObj[t])[0].boxstyle) {//requirenments for All Games section
            if ($(navObj[t])[0].menuTitle !== "Supplies" && $(navObj[t])[0].menuTitle !== "About Us") {
                $("#menuContent").html(selectBuildColumn[0] + "<div class=\"menu-more\"><i class=\"fa fa-caret-right\"></i> MORE RESULTS</div>"); //fill card links area 
                $('.menu-more').click(function () {
                    location.href = fixURL($(navObj[t])[0].url);
                });
            } else {
                $("#menuContent").html(selectBuildColumn[0]);
            }
            $(".end-panel").html("<div class=\"menu-column-content\">" + selectBuildColumn[1] + "</div>"); //fill sidebar
            if ($(navObj[t])[0].sections.length !== 1 && $(navObj[t])[0].menuTitle !== "Supplies") {
                $(".end-panel").css("display", "block");
        } else {
                $(".end-panel").css("display", "none");
        }
            $('.menu-first-column').css("display", "block");
            var firstSection = $(navObj[t])[0].sections[0]; //first section
            var mainTout = $(navObj[t])[0].sections[0].links[0]; //first item in first section
            $('.menu-first-column').html("<div class=\"clear-header\"><span class=\"clear-header-text\">" + firstSection.title + "</span></div><div class=\"menu-first-column-image\"><span class=\"image-helper\"></span><img class=\"main-img\" src=\"" + imagesFolder + "/" + mainTout.image + "\"/></div><div class=\"menu-bottom-wrap\"><div class=\"game-title\"><div id=\"main-inner-text\"><span>" + mainTout.title + "</span><div class=\"game-links\">" + mainTout.subcomp + "</div></div></div><div class=\"menu-bottom-links\"></div></div>");
            if ($(navObj[t])[0].menuTitle !== "About Us") {
                $(".menu-bottom-links").html("<div id=\"menu-as\" class=\"menu-large-button\"><i class=\"fa fa-search\"></i> ADVANCED SEARCH</div><div id=\"menu-pg\" class=\"menu-large-button menu-green\"><i class=\"fa fa-dollar\"></i> PRICE GUIDE</div>");
                $('.game-title').click(function () {
                    location.href = fixURL(mainTout.url);
                });
                $('.main-img').click(function () {
                    location.href = fixURL(mainTout.url);
                });
                $('#menu-as').click(function () {
                    location.href = fixURL($(navObj[t])[0].url);
                });
                if($(navObj[t])[0].guide!==""){
                    $('#menu-pg').css("display", "block");
                    $('.menu-bottom-wrap').css("bottom", "55px");
                    $('#menu-pg').click(function () {
                    location.href = fixURL($(navObj[t])[0].guide);
                    });
                } else {
                    $('#menu-pg').css("display", "none");
                    $('.menu-bottom-wrap').css("bottom", "98px");
                }
            } else {
                $(".menu-bottom-links").html("");
            }
        } else {
            $('.menu-first-column').css("display", "none");
            $(".end-panel").css("display", "none");
            $(".menu-bottom-links").html("");
        }
    } //end buildMenus
    function fixURL(u) {
        if(u.substr(0, 4)=="http"){
            
        }else{
            u = SECUREFULLSITEROOT + u.slice(1);
        }
        return u;
    }

    function suppliesBox(t) {
        $("#allgames_" + t).parent().css("background-color", "rgba(229,224,163,.5)");
    }
    function tcgBox(t) {
        $("#allgames_" + t).parent().css("background-color", "rgba(204,229,247,.5)");
    }

});
