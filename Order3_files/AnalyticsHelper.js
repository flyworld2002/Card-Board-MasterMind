// generic analytics methods
const analyticsHelper = {
    pageViewSent: false,
    previousPageData: {
        title: "",
        href: ""
    },
    application: 'dominaria',

    logEvent: function (eventName, eventData = {}) {
        eventData.application = this.application;
        eventData.domain = window.location.hostname;

        // send to Segment
        segmentHelper.trackEvent(eventName, eventData);
    },

    logRevenue: function (revenue) {
        // send to Segment
        segmentHelper.trackEvent('Revenue', { revenue: revenue });
    },

    /**
     * 
     */
    logoutUser: function () {
        // send to Segment
        segmentHelper.reset();
    },

    /**
     * Call on page unload to make sure it's a last event on the page.
     */
    trackGeneralPageViewEvent: function () {

        const cookieName = "tcg_analytics_previousPageData";
        const cookieValue = this.getCookieValue(cookieName);
        if (cookieValue) {
            this.previousPageData = JSON.parse(cookieValue);
        }

        if (this.pageViewSent === false) {
            const eventName = "Marketplace Page View";
            const eventData = {
                referrer: document.referrer,
                path: window.location.href,
                name: document.title,
                fromPath: this.previousPageData.href,
                fromName: this.previousPageData.title,
            };
            this.logEvent(eventName, eventData);
        }

        // Save current page data in cookie for next page before move forward.
        this.previousPageData.href = window.location.href;
        this.previousPageData.title = document.title;
        this.setCookie(cookieName, JSON.stringify(this.previousPageData));
    },

    getCookieValue(name) {
        // find cookie
        const cookie = document.cookie.split('; ').find(row => row.trim().startsWith(name + '='));

        if (cookie) {
            // get value
            return decodeURIComponent(cookie.split('=')[1]);
        }
        else {
            return null;
        }
    },

    setCookie(name, value) {
        document.cookie = name + "=" + encodeURIComponent(value) + "; domain=" + SITE_DOMAIN + "; path=/";
    },

    /**
     * @param {number} totalItemsCount Total items count
     * @param {number} directProductCount The total quantity of direct products in the order
     * @param {number} directOrderTotal The total dollar amount of direct products in the order
     * @param {string[]} productLines Product line (game) names
     * @param {number} totalPackagesCount Total packages count
     * @param {number} revenue Total order value
     * @param {number} orderId orderId
     * @param {string} cartKey cartKey associated to the cart
     * @param {number} productIds product IDs within the order
     * @param {number} productNames product names within the order
     * @param {string} paymentType The payment type used to complete the order
     * @param {string} paymentOrigin The page where the payment was created
     * @param {boolean} inContext Whether or not the event was sent from an InContext (pro seller storefront) page
     * @param {object[]} items The array of items data to send to impact.com from their integration with Segment
     * @param {string} currencyCode The three letter currency code
     * @param {string} shippingOption The shipping option chosen
     */
    trackOrderCompleteEvents: function (
        totalItemsCount,
        directProductCount,
        directOrderTotal,
        productLines,
        totalPackagesCount,
        revenue,
        orderId,
        cartKey,
        productIds,
        productNames,
        paymentType,
        paymentOrigin,
        sellersInCart,
        sellerKeys,
        productsInCart,
        inContext,
        items,
        currencyCode,
        shippingOption) {

        const eventName = 'Order Complete View';
        const eventData = {
            itemsInCart: totalItemsCount,
            productLinesInCart: productLines,
            packagesInCart: totalPackagesCount,
            orderTotal: revenue,
            orderId: orderId,
            cartKey: cartKey,
            productId: productIds,
            productName: productNames,
            paymentType: paymentType,
            paymentOrigin: paymentOrigin,
            salesChannel: inContext ? 'Storefront' : 'Marketplace',
            shippingOption: shippingOption,
            directProducts: directProductCount,
            directOrderTotal: directOrderTotal,
            sellersInCart: sellersInCart,
            sellerKeys: sellerKeys,
            productsInCart: productsInCart,
            currencyCode: currencyCode,
            items: items,
        };
        this.logEvent(eventName, eventData);
        this.pageViewSent = true;

        this.logRevenue(revenue);
    },

    /**
     * @param {number} totalItemsCount Total items count
     * @param {number} directProductCount The total quantity of direct products in the order
     * @param {number} directOrderTotal The total dollar amount of direct products in the order
     * @param {string[]} productLines Product line (game) names
     * @param {number} totalPackagesCount Total packages count
     * @param {number} revenue Total order value
     * @param {number} orderId orderId
     * @param {string} cartKey cartKey associated to the cart
     * @param {number} productIds product IDs within the order
     * @param {number} productNames product names within the order
     * @param {string} paymentType The payment type used to complete the order
     * @param {string} paymentOrigin The page where the payment was created
     * @param {boolean} inContext Whether or not the event was sent from an InContext (pro seller storefront) page
     * @param {string} shippingOption The shipping option chosen
     */
    trackOrderSubmittedEvents: function (
        totalItemsCount,
        productLines,
        totalPackagesCount,
        orderTotal,
        productIds,
        productNames,
        cartKey,
        paymentType,
        paymentOrigin,
        directProductCount,
        directOrderTotal,
        sellersInCart,
        sellerKeys,
        productsInCart,
        inContext)
    {
        const eventName = 'Order Submitted';
        const eventData = {
            itemsInCart: totalItemsCount,
            productLinesInCart: productLines,
            packagesInCart: totalPackagesCount,
            orderTotal: orderTotal,
            salesChannel: inContext ? 'Storefront' : 'Marketplace',
            productId: productIds,
            productName: productNames,
            cartKey: cartKey,
            paymentType: paymentType,
            paymentOrigin: paymentOrigin,
            directItems: directProductCount,
            directOrderTotal: directOrderTotal,
            sellersInCart: sellersInCart,
            sellerKeys: sellerKeys,
            productsInCart: productsInCart,
        };
        this.logEvent(eventName, eventData);
    },

    /**
     * @param {number} orderId orderId
     */
    ISPUPLOrderSubmitted: function (orderId) {
        const eventName = 'Storefronts Pay Later Order Submitted';
        const eventData = {
            orderId: orderId,
        };
        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
     * @param {string} cartKey The key associated with the current cart
     * @param {number} totalItemsCount Total items count
     * @param {number} totalPackagesCount Total packages count
     * @param {string[]} productLines Product line (game) names
     * @param {number} cartTotal Total value of all items in cart (item cost + shipping)
     * @param {number} directProducts Total number of direct products in cart
     * @param {number} directCartValue Total cost of Direct items in cart
     * @param {boolean} inContext Whether or not the event was sent from an InContext (pro seller storefront) cart
     */
    trackCartEvents: function (
        cartKey,
        totalItemsCount,
        totalPackagesCount,
        productLines,
        cartTotal,
        directProducts,
        directCartValue,
        sellersInCart,
        productsInCart,
        inContext) {
        const eventName = 'Cart View';
        const eventData = {
            cartKey: cartKey,
            itemsInCart: totalItemsCount,
            productLinesInCart: productLines,
            packagesInCart: totalPackagesCount,
            totalCartValue: cartTotal,
            directProducts: directProducts,
            directCartValue: directCartValue,
            sellersInCart: sellersInCart,
            productsInCart: productsInCart,
            salesChannel: inContext ? 'Storefront' : 'Marketplace',
        };
        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
     * @param {string} cartKey The key associated with the current cart
     * @param {number} totalItemsCount Total items count
     * @param {number} totalCartValue Total cart value, shipping + item cost
     * @param {string[]} productLines Product line (game / product category) names
     * @param {number} totalPackagesCount Total packages count
     * @param {number} distinctSKUs Distinct product condition id count
     */
    trackCartOptimizerViewEvent: function (cartKey, totalItemsCount, totalCartValue, productLines, totalPackagesCount, distinctSKUs) {

        const eventName = 'Cart Optimizer View';
        const eventData = {
            cartKey: cartKey,
            itemsInCart: totalItemsCount,
            totalCartValue: totalCartValue,
            productLinesInCart: productLines,
            packagesInCart: totalPackagesCount,
            distinctSkus: distinctSKUs,
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
    * @param {number} totalItemsCount Total items count
    * @param {number} totalCartValue Total cart value, shipping + item cost
    * @param {string[]} productLines Product line (game / product category) names
    * @param {number} totalPackagesCount Total packages count    
    * @param {number} distinctSKUs Distinct product condition id count
    * @param {string[]} preferences Array of top level preferences
    * @param {number} directCartPrice directCartPrice
    * @param {number} verifiedCartPrice verifiedCartPrice
    * @param {number} allSellersCartPrice allSellersCartPrice
    * @param {number} directPackages Total number of packages in direct cart
    * @param {number} verifiedPackages Total number of packages in verified cart
    * @param {number} allSellerPackages Total number of packages in all seller cart
    * @param {boolean} advancedOptimize The advanced options section was opened
    * @param {string[]} advancedOptions The advanced options filters that the user interacted with
    * @param {number} advancedItems The number of items modified via advanced options
    */
    trackCartOptimizerCompletedEvent: function (totalItemsCount, totalCartValue, productLines, totalPackagesCount, distinctSKUs,
        preferences, directCartPrice, verifiedCartPrice, allSellersCartPrice, directPackages, verifiedPackages, allSellerPackages,
        advancedOptimize, advancedOptions, advancedItems) {

        const eventName = 'Cart Optimizer Complete';
        const eventData = {
            itemsInCart: totalItemsCount,
            totalCartValue: totalCartValue,
            productLinesInCart: productLines,
            packagesInCart: totalPackagesCount,
            distinctSkus: distinctSKUs,
            preferences: preferences,
            directCartPrice: directCartPrice,
            verifiedCartPrice: verifiedCartPrice,
            allSellersCartPrice: allSellersCartPrice,
            directPackages: directPackages,
            verifiedPackages: verifiedPackages,
            allSellerPackages: allSellerPackages,
            advancedOptimize: advancedOptimize,
            advancedOptions: advancedOptions,
            advancedItems: advancedItems,
        };

        this.logEvent(eventName, eventData);
    },

    /**
     * @param {string} cartType what cart the user selects
     * @param {string} cartKey the key associated to the selected cart
     * @param {number} totalItemsCount Total items count
     * @param {number} totalCartValue Total cart value, shipping + item cost
     * @param {number} totalPackagesCount Total packages count
     * @param {number} distinctSKUs Distinct product condition id count
     */
    trackCartOptimizerSelectedEvent: function (cartType, cartKey, totalItemsCount, totalCartValue, totalPackagesCount, distinctSKUs) {

        const eventName = 'Cart Optimizer Select Cart';
        const eventData = {
            cartType: cartType,
            cartKey: cartKey,
            cartItems: totalItemsCount,
            totalCartValue: totalCartValue,
            packagesInCart: totalPackagesCount,
            distinctSkus: distinctSKUs,
        };

        this.logEvent(eventName, eventData);
    },

    trackItemAddedToCartViewEvent: function (cartKey, productId, productName) {

        const eventName = 'Item Added to Cart View';
        const eventData = {
            cartKey: cartKey,
            productId: productId,
            productName: productName,
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
    * @param {number} sellerSpotlightQuantity Seller spotlight available quantity
    * @param {string} productLine Product line name
    * @param {string} setName Product set name
    * @param {boolean} isSealed Product is sealed product
    * @param {boolean} isSingles Product is card single
    * @param {number} totalListings Total listings count
    * @param {number} totalSellers Total sellers count
    * @param {boolean} isNearMintAvailable NearMint contidion is available
    * @param {boolean} isLightlyPlayedAvailable LightlyPlayed contidion is available
    * @param {boolean} isModeratelyPlayedAvailable ModeratelyPlayed contidion is available
     */
    trackProductDetailsView: function (
        sellerSpotlightQuantity,
        productLine,
        setName,
        isSealed,
        isSingles,
        totalListings,
        totalSellers,
        isNearMintAvailable,
        isLightlyPlayedAvailable,
        isModeratelyPlayedAvailable) {

        const eventData = {
            sellerSpotlightQuantity: sellerSpotlightQuantity,
            productLine: productLine,
            setName: setName,
            isSealed: isSealed,
            isSingles: isSingles,
            totalListings: totalListings,
            totalSellers: totalSellers,
            isNearMintAvailable: isNearMintAvailable,
            isLightlyPlayedAvailable: isLightlyPlayedAvailable,
            isModeratelyPlayedAvailable: isModeratelyPlayedAvailable,
            newPdGroup: false,
        };

        this.logEvent('Product Details View', eventData);
        this.pageViewSent = true;
    },

    /**
    * @param {string} addToCartPosition 'Spotlight' or position in price table
    * @param {number} price Listing price
    * @param {string} condition Condition of card
    * @param {string[]} sellerTypes Array of seller associated badges
    * @param {boolean} sellerInCart Is seller in cart already or not
    * @param {number} page Listing current page
    * @param {boolean} isSealed Product is sealed product
    * @param {boolean} isSingles Product is card single
    * @param {boolean} shopBySeller The buyer is shopping from a specific seller
     */
    trackProductDetailsAddToCartEvent: function (
        addToCartPosition,
        price,
        condition,
        sellerTypes,
        sellerInCart,
        page,
        isSealed,
        isSingles,
        shopBySeller) {

        const eventData = {
            addToCartPosition: addToCartPosition,
            price: price,
            condition: condition,
            sellerType: sellerTypes,
            sellerInCart: sellerInCart,
            page: page,
            isSealed: isSealed,
            isSingles: isSingles,
            shopBySeller: shopBySeller,
            newPdGroup: false,
        };

        this.logEvent('Product Details Add to Cart', eventData);
    },

    /**
     * @param {string} cartKey The key associated with the user's cart
     * @param {boolean} inContext Whether or not the event was sent from an InContext (pro seller storefront) page
     */
    trackCheckoutShippingViewEvent: function (cartKey, inContext) {

        const eventName = 'Shipping Checkout View';
        const eventData = {
            cartKey: cartKey,
            salesChannel: inContext ? 'Storefront' : 'Marketplace',
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
     * @param {string} cartKey The key associated with the user's cart
     */
    trackSubscriptionUpsellClickedEvent: function (cartKey) {
        const eventName = 'Subscription Upsell Clicked';
        const eventData = {
            cartKey: cartKey,
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    trackCheckoutSubscriptionUpsellEnabled: function (cartKey) {
        const eventName = 'Checkout Subscription Upsell';
        const eventData = {
            cartKey: cartKey,
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
     * @param {string} cartKey The key associated with the user's cart
     */
    trackSubscriptionOptInChoice: function (cartKey, wantsSubscribe) {

        const eventName = 'Subscription Opt-In';
        const eventData = {
            cartKey: cartKey,
            subscribe: wantsSubscribe,
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
     * @param {string} cartKey The key associated with the user's cart
     * @param {number[]} productIds The IDs associated with the products in the user's cart
     * @param {string[]} productNames The names of the products in the user's cart
     * @param {boolean} inContext Whether or not the event was sent from an InContext (pro seller storefront) page
     */
    trackCheckoutReviewAndPayViewEvent: function (cartKey, productIds, productNames, inContext) {

        const eventName = 'Review and Pay Checkout View';
        const eventData = {
            cartKey: cartKey,
            productId: productIds,
            productName: productNames,
            salesChannel: inContext ? 'Storefront' : 'Marketplace',
        };

        this.logEvent(eventName, eventData);
        this.pageViewSent = true;
    },

    /**
    * @param {string} origin The page the sign up occurred on
    */
    trackMarketplaceEmailSignUp: function (origin) {
        const eventName = 'Marketplace Email Sign Up';
        const eventData = {
            origin: origin,
        };

        this.logEvent(eventName, eventData);
    },

    trackListoProductView(eventData) {
        const eventName = 'LISTO Product Details View';
        this.logEvent(eventName, eventData);
    },

    trackListoProductAddToCart(eventData) {
        const eventName = 'LISTO Product Details Add to Cart';
        this.logEvent(eventName, eventData);
    },

    /**
     * @param {string} cartKey identifier for a specific cart for a specific user
     * @param {number} savedPosition position presented in the list
     * @param {number} price listing price
     * @param {string} condition condition of card
     * @param {number} quantity number of units of the listing added to cart
     * @param {string} productType the product is a sealed product, secondary (singles), or gift card
     * @param {number} productId ID of that specific product in our catalog
     * @param {string} productName name of product
     * @param {string} productLine vertical of the product
     * @param {boolean} directEligible is the listing added to cart direct eligible
     */
    trackSavedForLaterAddedToCart: function(cartKey, savedPosition, price, condition, quantity, productType, productId, productName, productLine, directEligible) {
        const eventName = "Saved for Later Added to Cart";
        const eventData = {
            cartKey: cartKey,
            savedPosition: savedPosition,
            price: price,
            condition: condition,
            itemsAdded: quantity,
            productType: productType,
            productId: productId,
            productName: productName,
            productLine: productLine,
            directEligible: directEligible,
            application: "dominaria"
        };
        this.logEvent(eventName, eventData);
    },

    /**
     * @param {string} button Identifier for which "checkout" button was clicked
     */
    trackCheckoutClick: function(button) {
        const eventName = "Checkout Clicked";
        const eventData = {
            button: button,
            application: "dominaria"
        };
        this.logEvent(eventName, eventData);
    },

    /**
     * @param {string} cartKey identifier for a specific cart for a specific user
     * @param {number} productsInSaved total count of line items
     */
    trackSavedForLaterAddAll: function(cartKey, productsInSaved) {
        const eventName = "Saved for Later Add All";
        const eventData = {
            cartKey: cartKey,
            productsInSaved: productsInSaved
        };
        this.logEvent(eventName, eventData);
    },

    /**
     * @param {string} cartKey identifier for a specific cart for a specific user
     * @param {number} price listing price
     * @param {string} condition condition of card
     * @param {number} itemsRemoved number of units of the listing removed
     * @param {string} productType the product is a sealed product, secondary (singles), or gift card
     * @param {number} productId ID of that specific product in our catalog
     * @param {string} productName name of product
     * @param {string} productLine vertical of the product
     * @param {string} sellerKey seller key from URL if event occurred on a seller storefront
     * @param {string} sellerName the name of the seller
     * @param {boolean} directPackage is the listing in a direct package
     * @param {boolean} directEligible is the listing added to cart direct eligible
     */
    trackCartItemRemoved: function(cartKey, price, condition, itemsRemoved, productType, productId, productName, productLine, sellerKey, sellerName, directPackage, directEligible) {
        this.logEvent("Cart Item Removed", {
            cartKey: cartKey,
            price: price,
            condition: condition,
            itemsRemoved: itemsRemoved,
            productType: productType,
            productId: productId,
            productName: productName,
            productLine: productLine,
            sellerKey: sellerKey,
            sellerName: sellerName,
            directPackage: directPackage,
            directEligible: directEligible
        });
    },

    /**
     * @param {string} cartKey identifier for a specific cart for a specific user
     * @param {number} price listing price
     * @param {string} condition condition of card
     * @param {number} itemsSaved number of units of the listing saved for later
     * @param {string} productType the product is a sealed product, secondary (singles), or gift card
     * @param {number} productId ID of that specific product in our catalog
     * @param {string} productName name of product
     * @param {string} productLine vertical of the product
     * @param {string} sellerKey the name of the seller
     * @param {string} sellerName seller key from URL if event occurred on a seller storefront
     * @param {boolean} directPackage is the listing in a direct package
     * @param {boolean} directEligible is the listing added to cart direct eligible
     */
    trackCartItemSaved: function (cartKey, price, condition, itemsSaved, productType, productId, productName, productLine, sellerKey, sellerName, directPackage, directEligible) {
        this.logEvent("Cart Item Saved", {
            cartKey: cartKey,
            price: price,
            condition: condition,
            itemsSaved: itemsSaved,
            productType: productType,
            productId: productId,
            productName: productName,
            productLine: productLine,
            sellerKey: sellerKey,
            sellerName: sellerName,
            directPackage: directPackage,
            directEligible: directEligible
        });

    },

    /**
     * @param {string} cartKey identifier for a specific cart for a specific user
     * @param {number} savedPosition position presented in the list
     * @param {number} price listing price
     * @param {string} condition condition of card
     * @param {number} itemsRemoved number of units of the listing removed from saved for later
     * @param {string} productType the product is a sealed product, secondary (singles), or gift card
     * @param {number} productId ID of that specific product in our catalog
     * @param {string} productName name of product
     * @param {string} productLine vertical of the product
     * @param {boolean} directEligible is the listing removed from saved for later direct eligible
     */
    trackSavedForLaterRemoved: function (cartKey, savedPosition, price, condition, itemsRemoved, productType, productId, productName, productLine, directEligible) {
        this.logEvent("Saved for Later Removed", {
            cartKey: cartKey,
            savedPosition: savedPosition,
            price: price,
            condition: condition,
            itemsRemoved: itemsRemoved,
            productType: productType,
            productId: productId,
            productName: productName,
            productLine: productLine,
            directEligible: directEligible
        });
    },

    /**
     * @param {string} cartKey
     * @param {number} productsInSaved
     */
    trackSavedForLaterCleared: function(cartKey, productsInSaved) {
        this.logEvent("Saved for Later Cleared", {
            cartKey: cartKey,
            productsInSaved: productsInSaved
        });
    },

    /**
     * @param {string} subject
     * @param {string} shippingMethod
     * @param {string} sellerName
     */
    trackAccountMessageSent: function (subject, shippingMethod, sellerName) {
        this.logEvent("Account Message Sent", {
            subject: subject,
            shippingMethod: shippingMethod,
            sellerName: sellerName
        });
    },
    
    /**
     * @param {string} path
     * @param {string} linkUrl
     */
    trackFooterLinkClicked: function (path, linkUrl) {
        this.logEvent("Footer Clicked", {
            path: path,
            linkUrl: linkUrl
        });
    },

    /**
     * @param {string} domain
     * @param {string} path
     * @param {Array} sellerFilter
     * @param {string} productLineSelector
     */
    trackSellerSearch: function (domain, path, sellerFilter, productLineSelector) {
        const eventName = "Marketplace Seller Search Requested";
        const eventData = {
            domain: domain,
            path: path,
            sellerFilter: sellerFilter,
            productLineSelector: productLineSelector
        };
        this.logEvent(eventName, eventData);
    },

    trackShopSeller: function (domain, path, sellerKey, sellerName) {
        const eventName = "Marketplace Seller Search Shop Seller Selected";
        const eventData = {
            domain: domain,
            path: path,
            sellerKey: sellerKey,
            sellerName: sellerName
        };
        this.logEvent(eventName, eventData);
    }
}
window.analyticsHelper = analyticsHelper;
